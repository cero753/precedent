"""Event -> derived memory. Every derived row records the events it came from (lineage)."""
import json
from datetime import date, datetime, timedelta

from . import extract as ex
from . import truth
from .db import audit, link, now, person, rows
from .embed import embed

# Default prediction attached at decision time from a per-action template; the approver can edit it.
# Every template is falsifiable (non-zero expected effect, fixed horizon).
HYPOTHESES = {
    "pause": {"metric": "blended_cpa", "expected_rel": -0.10, "horizon_days": 7, "leading": "ctr", "source": "template"},
    "resume": {"metric": "blended_cpa", "expected_rel": -0.05, "horizon_days": 7, "leading": "ctr", "source": "template"},
    "budget_change": {"metric": "cpa", "expected_rel": -0.05, "horizon_days": 7, "leading": "cpm", "source": "template"},
}


def window_for(conn, ev):
    t = datetime.fromisoformat(ev["ts"])
    lo = (t - timedelta(days=2)).isoformat(timespec="minutes")
    return rows(conn,
        "SELECT * FROM events WHERE client_id=? AND ts<=? AND ("
        " thread_id=? OR (channel='meeting' AND ts>=? AND (campaign_id=? OR campaign_id IS NULL)))"
        " ORDER BY ts", (ev["client_id"], ev["ts"], ev["thread_id"], lo, ev["campaign_id"]))


def ingest_event(conn, ev):
    eid, cid = ev["id"], ev["client_id"]
    # chunk + embedding + graph edges, each linked to its source event
    conn.execute("INSERT INTO chunks VALUES (?,?,?)", (f"ch_{eid}", cid, ev["text"]))
    link(conn, f"ch_{eid}", "chunks", [eid])
    conn.execute("INSERT INTO embeddings VALUES (?,?,?,?)", (f"emb_{eid}", cid, f"ch_{eid}", json.dumps(embed(ev["text"]))))
    link(conn, f"emb_{eid}", "embeddings", [eid])
    edges = [(f"edge_{eid}_from", cid, eid, "AUTHORED_BY", ev["author"])]
    if ev["campaign_id"]:
        edges.append((f"edge_{eid}_about", cid, eid, "MENTIONS", ev["campaign_id"]))
    for e in edges:
        conn.execute("INSERT INTO edges VALUES (?,?,?,?,?)", e)
        link(conn, e[0], "edges", [eid])

    if ev["channel"] == "ledger_import":
        return
    window = window_for(conn, ev)
    prior = rows(conn, "SELECT * FROM decisions WHERE client_id=? AND valid_from<? ORDER BY valid_from", (cid, ev["ts"]))
    signal = truth.trailing_cpa_signal(conn, ev["campaign_id"], ev["ts"]) if ev["campaign_id"] else None
    out, extractor = ex.extract(ev, window, prior, signal)
    if out.get("memory_class") == "policy" and ev["author_role"] != "agency":
        # enforced after any backend, LLM included: clients cannot write agency-wide policy
        out.update(memory_class="lasting_preference", scope="client", confidence=min(out["confidence"], 0.6))
    if not out.get("is_decision"):
        if out.get("confidence", 1) < 0.7 and ev["author_role"] == "client" and ev["channel"] in ("slack", "email"):
            qid = f"q_{eid}"
            conn.execute("INSERT INTO clarifications VALUES (?,?,?,?,?,?)",
                         (qid, cid, None, f"Possible instruction not understood from {ev['author']}: is this a request we should act on?",
                          None, "open"))
            link(conn, qid, "clarifications", [eid])
        return
    if out["confidence"] < 0.7 and not out.get("clarifying_question"):
        out["clarifying_question"] = (f"{ev['author']} said: \"{ev['text'][:80]}\". Is this a one-time request, a standing "
                                      f"preference for {cid}, or not an instruction?")
    did = f"d_{eid}"
    parents = [w["id"] for w in window]
    evidence = [{"type": "source", "event_id": w["id"], "channel": w["channel"]} for w in window]
    if signal:
        evidence.append({"type": "fact_snapshot", **signal})
    status = "needs_clarification" if out.get("clarifying_question") else "recorded"
    conn.execute(
        "INSERT INTO decisions (id, client_id, campaign_id, action, decided_by, valid_from, recorded_at, stated_reason,"
        " evidence, scope, scope_confidence, memory_class, trigger_condition, hypothesis, outcome, causal_conclusion,"
        " status, extractor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, cid, ev["campaign_id"], out["action"], ev["author"], ev["ts"], now(),
         json.dumps({"text": out.get("stated_reason"), "source_event": eid, "implicit": out.get("implicit_reason")}),
         json.dumps(evidence), out["scope"], out["confidence"], out["memory_class"], out.get("trigger_condition"),
         json.dumps(HYPOTHESES.get(out["action"])), None, None, status, extractor))
    link(conn, did, "decisions", parents)
    if ev["campaign_id"]:
        conn.execute("INSERT INTO edges VALUES (?,?,?,?,?)", (f"edge_{did}_about", cid, did, "ABOUT", ev["campaign_id"]))
        link(conn, f"edge_{did}_about", "edges", parents)

    if out.get("clarifying_question"):
        conn.execute("INSERT INTO clarifications VALUES (?,?,?,?,?,?)",
                     (f"q_{did}", cid, did, out["clarifying_question"], None, "open"))
        link(conn, f"q_{did}", "clarifications", parents)

    if out["memory_class"] in ("lasting_preference", "policy"):
        pid = f"p_{eid}"
        conn.execute("INSERT INTO preferences VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (pid, cid if out["memory_class"] != "policy" else "*", out["scope"], ev["text"],
                      out["memory_class"], "proposed", None, ev["ts"][:10], None, None,
                      json.dumps(ex.compile_constraint(ev["text"]))))
        link(conn, pid, "preferences", parents)

    if out["memory_class"] == "context_change":
        prev = conn.execute("SELECT id FROM context_versions WHERE client_id=? AND key='target_customer'"
                            " ORDER BY recorded_at DESC LIMIT 1", (cid,)).fetchone()
        ctx_id = f"ctx_{eid}"
        conn.execute("INSERT INTO context_versions VALUES (?,?,?,?,?,?,?)",
                     (ctx_id, cid, "target_customer", ev["text"], ev["ts"][:10], ev["ts"] + ":00",
                      prev["id"] if prev else None))
        link(conn, ctx_id, "context_versions", [eid])
        if prev:
            audit(conn, "ingest", "context_superseded", {"old": prev["id"], "new": ctx_id}, cid)
            # pending work proposed under the old context goes back to a person
            for a in rows(conn, "SELECT id, escalation_reasons FROM actions WHERE client_id=? AND status IN ('awaiting_approval','drafted')", (cid,)):
                reasons = json.loads(a["escalation_reasons"] or "[]") + [f"proposed before context change {ctx_id}; re-check"]
                conn.execute("UPDATE actions SET escalation_reasons=?, status='awaiting_approval' WHERE id=?", (json.dumps(reasons), a["id"]))
    audit(conn, "extractor", "decision_recorded", {"decision": did, "extractor": extractor, "status": status}, cid)


def ingest_all(conn):
    for ev in rows(conn, "SELECT * FROM events WHERE id NOT IN (SELECT parent_id FROM lineage WHERE child_table='chunks') ORDER BY ts"):
        ingest_event(conn, ev)
    conn.commit()


def confirm_preference(conn, pref_id, confirmed_by, scope=None, expires_days=180):
    """Role-checked: agency-wide policy needs the agency lead; client preferences need someone authorised for that client.
    A preference that can't be compiled to a typed constraint is stored as advisory: shown to people, never enforced."""
    p = conn.execute("SELECT * FROM preferences WHERE id=?", (pref_id,)).fetchone()
    if not p:
        raise KeyError(f"no preference {pref_id}")
    who = person(conn, confirmed_by)
    if not who:
        raise PermissionError(f"{confirmed_by} is not a known approver")
    if p["client_id"] == "*" and who["role"] != "agency_lead":
        raise PermissionError("agency-wide policy can only be confirmed by the agency lead")
    if who["client_id"] not in (p["client_id"], "*"):
        raise PermissionError(f"{confirmed_by} cannot confirm preferences for {p['client_id']}")
    status = "active" if json.loads(p["compiled"] or "{}") else "advisory"
    expires = (date.fromisoformat(now()[:10]) + timedelta(days=expires_days)).isoformat()
    conn.execute("UPDATE preferences SET status=?, confirmed_by=?, scope=COALESCE(?, scope), expires=? WHERE id=?",
                 (status, who["person"], scope, expires, pref_id))
    audit(conn, who["person"], "preference_confirmed", {"preference": pref_id, "status": status, "scope": scope},
          p["client_id"] if p["client_id"] != "*" else None)
    conn.commit()
    return status


def answer_clarification(conn, q_id, answer, scope, memory_class):
    q = conn.execute("SELECT * FROM clarifications WHERE id=?", (q_id,)).fetchone()
    conn.execute("UPDATE clarifications SET answer=?, status='answered' WHERE id=?", (answer, q_id))
    if q["decision_id"]:
        conn.execute("UPDATE decisions SET scope=?, memory_class=?, scope_confidence=1.0, status='recorded' WHERE id=?",
                     (scope, memory_class, q["decision_id"]))
    audit(conn, "account_manager", "clarification_answered", {"q": q_id, "scope": scope, "label": memory_class}, q["client_id"])
    conn.commit()
