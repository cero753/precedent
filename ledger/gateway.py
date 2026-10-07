"""Action gateway: the model proposes, this code decides. Risk tiers, escalation, approvals, rollback."""
import json
import re
import uuid

from .db import audit, norm_person, now, person, rows
from .embed import cosine, embed
from .extract import classify_comment

AUTO_BUDGET_BAND = 0.15
DAILY_CAP = 1000.0
TWO_PERSON_ABOVE = 1000.0
CONFIDENCE_FLOOR = 0.7
NOVELTY_FLOOR = 0.05  # set by hand for the toy hashing embedder; must be calibrated on real embeddings

TIER_RULES = {
    "analyze": "T0", "report": "T0",
    "draft_brief": "T1", "draft_email": "T1", "draft_creator_message": "T1",
    "recommend": "T2",
    "pause": "T4", "resume": "T4", "launch": "T4", "send_client_email": "T4", "whitelist_creator": "T4",
}
FORBIDDEN = {"send_creator_message": "Creators only receive messages sent by a person. Use draft_creator_message instead."}
APPROVAL_TIERS = {"T2", "T4"}
TEXT_KINDS = {"draft_brief", "draft_email", "draft_creator_message", "send_client_email"}


def tier_for(kind, params, campaign):
    if kind == "budget_change":
        if not campaign:
            return "T4"
        old, new = campaign["daily_budget"], params["new_daily_budget"]
        change = abs(new - old) / old if old else 1.0
        return "T3" if change <= AUTO_BUDGET_BAND and new <= DAILY_CAP else "T4"
    return TIER_RULES.get(kind, "T4")  # unknown actions get the strictest tier


def compensating_for(kind, params, campaign):
    if kind in ("pause", "resume", "launch") and campaign:
        return {"kind": "set_status", "status": campaign["status"]}
    if kind == "budget_change" and campaign:
        return {"kind": "set_budget", "daily_budget": campaign["daily_budget"]}
    if kind.startswith("draft"):
        return {"kind": "discard_draft"}
    return None


def preference_conflicts(conn, client_id, kind, params, campaign):
    """Only active, confirmed, enforceable constraints bind. Advisory preferences are shown, never enforced."""
    hits = []
    for p in rows(conn, "SELECT * FROM preferences WHERE status='active' AND (client_id=? OR client_id='*')"
                        " AND (expires IS NULL OR expires>=?)", (client_id, now()[:10])):
        c = json.loads(p["compiled"] or "{}")
        target = ((campaign or {}).get("campaign_id", "") + " " + (campaign or {}).get("name", "")).lower()
        if c.get("applies_to") and c["applies_to"] not in target:
            continue
        if c.get("action") and c["action"] != kind:
            continue
        if c.get("forbid_days") and kind in ("resume", "launch", "budget_change"):
            days = {d.lower() for d in params.get("run_days", [])}
            if not days or days & set(c["forbid_days"]):
                hits.append(f"conflicts with {p['kind']} {p['id']}: forbid {c['forbid_days']} on {c.get('applies_to', 'all')}")
        if c.get("require_param") and not params.get(c["require_param"]):
            hits.append(f"conflicts with {p['kind']} {p['id']}: requires {c['require_param']}")
        if c.get("forbid_terms") and kind in TEXT_KINDS:
            text = str(params.get("text", "")).lower()
            bad = [t for t in c["forbid_terms"] if t in text]
            if bad:
                hits.append(f"conflicts with {p['kind']} {p['id']}: uses forbidden term(s) {bad}")
    return hits


def novelty(conn, client_id, text):
    v = embed(text)
    best = 0.0
    for r in rows(conn, "SELECT vector FROM embeddings WHERE client_id=?", (client_id,)):
        best = max(best, cosine(v, json.loads(r["vector"])))
    return best


def assess(conn, client_id, kind, params, campaign, confidence, evidence, rationale):
    """Deterministic risk assessment. Re-run on every change to params, so a reply can't smuggle in a bigger action."""
    tier = tier_for(kind, params, campaign)
    reasons = []
    if tier in APPROVAL_TIERS:
        reasons.append(f"tier {tier} requires human approval")
    if confidence < CONFIDENCE_FLOOR:
        reasons.append(f"confidence {confidence:.2f} below {CONFIDENCE_FLOOR}")
    reasons += preference_conflicts(conn, client_id, kind, params, campaign)
    if any(e.get("divergence_flag") for e in evidence):
        reasons.append("ad platform vs CRM gap moved outside its normal range on a metric used as evidence")
    sim = novelty(conn, client_id, f"{kind} {rationale}")
    if sim < NOVELTY_FLOOR:
        reasons.append(f"novel situation (max similarity to past context {sim:.2f})")
    if kind == "budget_change" and params.get("new_daily_budget", 0) > TWO_PERSON_ABOVE:
        reasons.append(f"budget above ${TWO_PERSON_ABOVE:.0f}/day needs two approvers")
    return tier, reasons


def _campaign(conn, campaign_id):
    c = conn.execute("SELECT * FROM campaign_state WHERE campaign_id=?", (campaign_id,)).fetchone()
    return dict(c) if c else None


def propose(conn, client_id, kind, params=None, campaign_id=None, rationale="", confidence=0.9,
            evidence=None, proposed_by="planner_agent"):
    params, evidence = dict(params or {}), evidence or []
    params["_v"] = 1
    campaign = _campaign(conn, campaign_id)
    aid = "act_" + uuid.uuid4().hex[:8]
    if kind in FORBIDDEN:
        conn.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (aid, client_id, campaign_id, kind, json.dumps(params), "BLOCKED", "blocked", proposed_by,
                      rationale, confidence, json.dumps(evidence), json.dumps([FORBIDDEN[kind]]), None, now()))
        audit(conn, "gateway", "action_blocked", {"action": aid, "kind": kind}, client_id)
        conn.commit()
        return get(conn, aid)

    tier, reasons = assess(conn, client_id, kind, params, campaign, confidence, evidence, rationale)
    if tier in ("T0", "T1") and not reasons:
        status = "drafted" if tier == "T1" else "done"
    elif tier == "T3" and not reasons:
        status = "auto_approved"
    else:
        status = "awaiting_approval"
    conn.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (aid, client_id, campaign_id, kind, json.dumps(params), tier, status, proposed_by, rationale,
                  confidence, json.dumps(evidence), json.dumps(reasons),
                  json.dumps(compensating_for(kind, params, campaign)), now()))
    audit(conn, "gateway", "action_proposed", {"action": aid, "tier": tier, "status": status, "reasons": reasons}, client_id)
    if status == "auto_approved":
        execute(conn, aid, actor="gateway(auto T3)")
    conn.commit()
    return get(conn, aid)


def get(conn, aid):
    r = dict(conn.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone())
    for k in ("params", "evidence", "escalation_reasons", "compensating"):
        r[k] = json.loads(r[k]) if r[k] else None
    return r


def _apply(conn, campaign_id, kind, params):
    if kind == "pause":
        conn.execute("UPDATE campaign_state SET status='paused' WHERE campaign_id=?", (campaign_id,))
    elif kind in ("resume", "launch"):
        conn.execute("UPDATE campaign_state SET status='active' WHERE campaign_id=?", (campaign_id,))
    elif kind == "budget_change":
        conn.execute("UPDATE campaign_state SET daily_budget=? WHERE campaign_id=?", (params["new_daily_budget"], campaign_id))
    elif kind == "set_status":
        conn.execute("UPDATE campaign_state SET status=? WHERE campaign_id=?", (params["status"], campaign_id))
    elif kind == "set_budget":
        conn.execute("UPDATE campaign_state SET daily_budget=? WHERE campaign_id=?", (params["daily_budget"], campaign_id))


def execute(conn, aid, actor="gateway"):
    a = get(conn, aid)
    if a["status"] not in ("approved", "auto_approved"):
        raise PermissionError(f"{aid} is {a['status']}; only approved actions execute")
    _apply(conn, a["campaign_id"], a["kind"], a["params"])
    conn.execute("UPDATE actions SET status='executed' WHERE id=?", (aid,))
    audit(conn, actor, "action_executed", {"action": aid, "kind": a["kind"]}, a["client_id"])
    conn.commit()
    return get(conn, aid)


def rollback(conn, aid, reason):
    a = get(conn, aid)
    if a["status"] != "executed" or not a["compensating"]:
        raise ValueError(f"{aid} cannot be rolled back (status {a['status']})")
    comp = a["compensating"]
    _apply(conn, a["campaign_id"], comp["kind"], comp)
    conn.execute("UPDATE actions SET status='rolled_back' WHERE id=?", (aid,))
    audit(conn, "gateway", "action_rolled_back", {"action": aid, "reason": reason}, a["client_id"])
    conn.commit()
    return get(conn, aid)


def watchdog(conn, aid, observed_cpa, guard_cpa):
    """T3 auto-actions run under a guard metric; breach -> automatic revert."""
    a = get(conn, aid)
    if a["status"] == "executed" and a["tier"] == "T3" and observed_cpa > guard_cpa:
        return rollback(conn, aid, f"guard breached: CPA {observed_cpa} > {guard_cpa}")
    return a


NEGATED_APPROVAL = re.compile(r"\b(not|isn'?t|is not)\s+(ok(ay)?|approved?|good|fine|happy)\b|\bno[- ]go\b", re.I)
APPROVE = re.compile(r"\b(yes|yep|approved?|go ahead|go for it|lgtm|ship it|sounds good|ok(ay)?|confirmed)\b|👍|✅", re.I)
REJECT = re.compile(r"\b(no|nope|don'?t|do not|hold off|reject(ed)?|not now|cancel|stop)\b|👎", re.I)
AMBIG = re.compile(r"\b(hmm+|not sure|let'?s discuss|maybe|let me think|unsure|can we talk)\b|\?$", re.I)
CAP = re.compile(r"\b(?:cap(?:ped)?|limit|max(?:imum)?)\b[^0-9$]*\$?\s?(\d[\d,]*)", re.I)


def parse_reply(text, kind=None):
    """Fail closed: mixed or unclear signals are 'ambiguous', which never executes anything."""
    t = re.sub(r"\bno (problem|worries|issues?)\b", "ok", text.strip(), flags=re.I)
    if AMBIG.search(t):
        return {"verdict": "ambiguous", "params": {}}
    if NEGATED_APPROVAL.search(t):
        return {"verdict": "reject", "params": {}}
    yes, no = APPROVE.search(t), REJECT.search(t)
    if yes and no:
        return {"verdict": "ambiguous", "params": {}}
    if no:
        return {"verdict": "reject", "params": {}}
    if yes:
        cap = CAP.search(t) if kind in (None, "budget_change") else None
        if cap:
            return {"verdict": "modify", "params": {"new_daily_budget": float(cap.group(1).replace(",", ""))}}
        return {"verdict": "approve", "params": {}}
    return {"verdict": "ambiguous", "params": {}}


def can_approve(conn, a, approver):
    """Approver must be a known person, authorised for this client, and not the proposer."""
    p = person(conn, approver)
    if not p:
        return False, "unknown approver"
    if p["client_id"] not in (a["client_id"], "*"):
        return False, f"{p['person']} is not authorised for client {a['client_id']}"
    if norm_person(approver) == norm_person(a["proposed_by"]):
        return False, "proposer cannot approve their own action"
    return True, None


def record_reply(conn, aid, approver, text, channel="slack"):
    a = get(conn, aid)
    parsed = parse_reply(text, a["kind"])
    who = norm_person(approver)
    conn.execute("INSERT INTO approvals (client_id, action_id, approver, verdict, raw_text, parsed, ts) VALUES (?,?,?,?,?,?,?)",
                 (a["client_id"], aid, who, parsed["verdict"], text, json.dumps({**parsed, "v": a["params"]["_v"]}), now()))
    v = parsed["verdict"]
    ok, why = can_approve(conn, a, approver)
    if a["status"] != "awaiting_approval":
        audit(conn, who, "late_reply_ignored", {"action": aid, "status": a["status"]}, a["client_id"])
    elif not ok and v in ("approve", "modify"):
        audit(conn, "gateway", "approval_refused", {"action": aid, "approver": who, "why": why}, a["client_id"])
    elif v == "ambiguous":
        audit(conn, "gateway", "approval_ambiguous", {"action": aid, "follow_up": "asked approver to reply yes or no"}, a["client_id"])
    elif v == "reject":
        conn.execute("UPDATE actions SET status='rejected' WHERE id=?", (aid,))
        audit(conn, who, "action_rejected", {"action": aid, "via": channel}, a["client_id"])
    else:
        if v == "modify":
            params = {**a["params"], **parsed["params"], "_v": a["params"]["_v"] + 1}
            tier, reasons = assess(conn, a["client_id"], a["kind"], params, _campaign(conn, a["campaign_id"]),
                                   a["confidence"], a["evidence"] or [], a["rationale"])
            conn.execute("UPDATE actions SET params=?, tier=?, escalation_reasons=? WHERE id=?",
                         (json.dumps(params), tier, json.dumps(reasons), aid))
            conn.execute("UPDATE approvals SET parsed=json_set(parsed, '$.v', ?) WHERE id=(SELECT MAX(id) FROM approvals WHERE action_id=?)",
                         (params["_v"], aid))
            audit(conn, "gateway", "action_modified_reassessed", {"action": aid, "tier": tier, "reasons": reasons}, a["client_id"])
            a = get(conn, aid)
        needs_two = any("two approvers" in r for r in a["escalation_reasons"] or [])
        valid = set()
        for r in rows(conn, "SELECT approver, parsed FROM approvals WHERE action_id=? AND verdict IN ('approve','modify')", (aid,)):
            if json.loads(r["parsed"]).get("v") == a["params"]["_v"] and can_approve(conn, a, r["approver"])[0]:
                valid.add(r["approver"])  # only approvals of the current version of the params count
        if needs_two and len(valid) < 2:
            audit(conn, "gateway", "awaiting_second_approver", {"action": aid, "have": sorted(valid)}, a["client_id"])
        else:
            conn.execute("UPDATE actions SET status='approved' WHERE id=?", (aid,))
            audit(conn, who, "action_approved", {"action": aid, "via": channel, "verdict": v}, a["client_id"])
            conn.commit()
            return execute(conn, aid, actor=who), parsed
    conn.commit()
    return get(conn, aid), parsed


def comment_gate(text, author_role="client"):
    """Route a comment before anything enters memory."""
    c = classify_comment(text, author_role)
    route = {
        "one_off": "decision ledger only (no lasting memory)",
        "lasting_preference": "propose preference; stored only after an authorised person confirms scope",
        "policy": "propose agency policy; stored only after the agency lead confirms",
        "context_change": "propose new context version; dependent work flagged for review",
        "none": "not a decision; keep as conversation context",
    }[c["memory_class"]]
    if c["confidence"] < CONFIDENCE_FLOOR:
        route = "ask account manager before routing: " + route
    return {**c, "route": route}
