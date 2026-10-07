"""Decision evaluation: process vs outcome, attribution with stated method, pooled decision classes."""
import json
import math
import random
import zlib
from datetime import date, timedelta
from statistics import mean

from . import truth
from .db import audit, rows

LOWER_IS_BETTER = {"cpa", "blended_cpa"}


BLOCK = 3  # days; resampling blocks keeps short-run autocorrelation that a day-level bootstrap would destroy


def _block_resample(v, rng):
    out = []
    while len(out) < len(v):
        s = rng.randrange(0, len(v) - BLOCK + 1)
        out.extend(v[s:s + BLOCK])
    return out[:len(v)]


def did_effect(obs, n_boot=400, seed=0):
    """Relative difference-in-differences with a moving-block bootstrap CI."""
    def eff(tp, tq, cp, cq):
        return (mean(tq) / mean(tp)) / (mean(cq) / mean(cp)) - 1

    point = eff(obs["treated_pre"], obs["treated_post"], obs["control_pre"], obs["control_post"])
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        s = {k: _block_resample(v, rng) for k, v in obs.items()}
        draws.append(eff(s["treated_pre"], s["treated_post"], s["control_pre"], s["control_post"]))
    draws.sort()
    return point, (draws[int(0.025 * n_boot)], draws[int(0.975 * n_boot)])


def process_score(checklist: dict) -> float:
    return round(sum(bool(v) for v in checklist.values()) / max(len(checklist), 1), 2)


def outcome_call(metric, ci):
    """good / bad only when the whole CI agrees; otherwise the data can't tell, and we say so."""
    lo, hi = ci
    if metric in LOWER_IS_BETTER:
        lo, hi = -hi, -lo
    return "good" if lo > 0 else ("bad" if hi < 0 else "unclear")


def quadrant(proc: float, outcome: str) -> str:
    good_proc = proc >= 0.75
    if outcome == "unclear":
        return "inconclusive"
    return {(True, "good"): "earned_win", (True, "bad"): "bad_luck",
            (False, "good"): "lucky", (False, "bad"): "earned_loss"}[(good_proc, outcome)]


def evaluate_decision(conn, d, today="2026-10-06"):
    hyp = json.loads(d["hypothesis"] or "null")
    if not hyp:
        return None
    outcome = json.loads(d["outcome"] or "null") or {}
    obs = outcome.get("observations")
    proc = process_score(hyp.get("process", {"evidence_attached": True, "hypothesis_preregistered": True}))
    due = (date.fromisoformat(d["valid_from"][:10]) + timedelta(days=hyp["horizon_days"])).isoformat()

    if obs:
        eff, ci = did_effect(obs, seed=zlib.crc32(d["id"].encode()))
        call = outcome_call(hyp["metric"], ci)
        concl = {"effect_rel": round(eff, 3), "ci95": [round(ci[0], 3), round(ci[1], 3)],
                 "method": "difference-in-differences vs matched control campaign, 3-day block bootstrap",
                 "outcome": call, "expected_rel": hyp["expected_rel"]}
        result = {"status": "final", "process": proc, "quadrant": quadrant(proc, call), "conclusion": concl}
    elif today < due:
        leading = None
        if hyp.get("leading") == "organic_sessions":
            leading = lift_above_trend(conn, "nw_seo", "organic_sessions", "analytics",
                                       date.fromisoformat(d["valid_from"][:10]), date.fromisoformat(today))
        result = {"status": "interim", "process": proc, "due": due,
                  "conclusion": {"leading_indicator": hyp.get("leading"), **(leading or {}),
                                 "method": "leading indicator vs pre-period trend projection; final verdict at horizon"}}
    else:
        result = {"status": "pending", "process": proc, "due": due, "conclusion": None}

    conn.execute("UPDATE decisions SET status=?, causal_conclusion=? WHERE id=?",
                 (result["status"], json.dumps(result), d["id"]))
    return result


def lift_above_trend(conn, campaign_id, metric, source, start, today, pre_days=28, post_days=14):
    """Raw growth is not lift: fit a line to the pre-period (as known today) and compare the latest window to its projection."""
    pre = truth.series(conn, campaign_id, metric, (start - timedelta(days=pre_days)).isoformat(),
                       (start - timedelta(days=1)).isoformat(), source, today.isoformat())
    post = truth.series(conn, campaign_id, metric, (today - timedelta(days=post_days - 1)).isoformat(),
                        today.isoformat(), source, today.isoformat())
    if len(pre) < 7 or not post:
        return None
    xs = [(date.fromisoformat(k) - start).days for k in pre]
    ys = list(pre.values())
    mx, my = mean(xs), mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    proj = mean(my + slope * ((date.fromisoformat(k) - start).days - mx) for k in post)
    actual = mean(post.values())
    return {"raw_change_vs_pre": round(actual / my - 1, 3), "lift_above_trend": round(actual / proj - 1, 3)}


def live_process_checklist(conn, d):
    """Process judged only on what was knowable at decision time."""
    ev = json.loads(d["evidence"] or "[]")
    snap = next((e for e in ev if e.get("type") == "fact_snapshot"), None)
    return {
        "evidence_attached": snap is not None,
        "reason_stated": bool(json.loads(d["stated_reason"] or "{}").get("text")),
        "scope_resolved": d["status"] != "needs_clarification",
        "sources_agreed_at_time": bool(snap) and not snap.get("divergence_flag"),
    }


def evaluate_live_pause(conn, d, today):
    """Live pause with no holdout: fall down the attribution ladder to pre/post on the client's other spend, and say so."""
    hyp = json.loads(d["hypothesis"] or "{}")
    v0 = date.fromisoformat(d["valid_from"][:10])
    due = (v0 + timedelta(days=hyp.get("horizon_days", 7))).isoformat()
    check = live_process_checklist(conn, d)
    if today < due:
        return {"status": "pending", "process": process_score(check), "process_checklist": check, "due": due, "conclusion": None}
    siblings = [r["campaign_id"] for r in rows(conn, "SELECT campaign_id FROM campaign_state WHERE client_id=? AND campaign_id!=?"
                                                     " AND channel!='organic'", (d["client_id"], d["campaign_id"]))]
    known = today + "T23:59:59"  # only facts recorded by the evaluation date: no look-ahead to later restatements
    before = lambda c: truth.cpa(conn, c, (v0 - timedelta(days=7)).isoformat(), (v0 - timedelta(days=1)).isoformat(), known)["cpa"]  # noqa: E731
    after = lambda c: truth.cpa(conn, c, v0.isoformat(), (v0 + timedelta(days=6)).isoformat(), known)["cpa"]  # noqa: E731
    conv = truth.resolve(conn, d["campaign_id"], "conversions", (v0 - timedelta(days=7)).isoformat(), v0.isoformat(), known)
    res = {"status": "final", "process": process_score(check), "process_checklist": check, "conclusion": {
        "method": "pre/post on the client's remaining campaigns (no holdout or matched control: weakest rung)",
        "facts_as_of": known, "definitions_used": conv["definitions"], "source_used": conv["chosen_source"],
        "paused_campaign_cpa_before": before(d["campaign_id"]),
        "other_campaigns": {c: {"cpa_before": before(c), "cpa_after": after(c)} for c in siblings},
        "confidence": "low",
        "recommendation": "Not attributable without a control. Next similar decision: pause in half the regions (geo split)."}}
    return res


def evaluate_all(conn, today="2026-10-06"):
    out = {}
    for d in rows(conn, "SELECT * FROM decisions"):
        if d["extractor"] == "import":
            out[d["id"]] = evaluate_decision(conn, d, today)
        elif d["action"] == "pause" and d["campaign_id"]:
            out[d["id"]] = evaluate_live_pause(conn, d, today)
            conn.execute("UPDATE decisions SET causal_conclusion=? WHERE id=?", (json.dumps(out[d["id"]]), d["id"]))
    audit(conn, "evaluator", "evaluation_run", {"decisions": len(out), "as_of": today})
    conn.commit()
    return out


def class_stats(conn):
    """Pool decisions of the same class with a random-effects (DerSimonian-Laird) model and shrink each instance."""
    by_class = {}
    for d in rows(conn, "SELECT * FROM decisions WHERE status='final' AND extractor='import'"):
        r = json.loads(d["causal_conclusion"])
        c = r["conclusion"]
        se = max((c["ci95"][1] - c["ci95"][0]) / 3.92, 1e-4)
        hyp = json.loads(d["hypothesis"])
        by_class.setdefault(hyp["class"], []).append((d["id"], c["effect_rel"], se, c["expected_rel"], r["quadrant"], hyp["metric"]))
    out = {}
    for cls, items in by_class.items():
        y = [i[1] for i in items]
        w = [1 / i[2] ** 2 for i in items]
        fixed = sum(wi * yi for wi, yi in zip(w, y)) / sum(w)
        q = sum(wi * (yi - fixed) ** 2 for wi, yi in zip(w, y))
        c = sum(w) - sum(wi ** 2 for wi in w) / sum(w)
        tau2 = max(0.0, (q - (len(y) - 1)) / c) if c > 0 else 0.0
        wr = [1 / (i[2] ** 2 + tau2) for i in items]
        pooled = sum(wi * yi for wi, yi in zip(wr, y)) / sum(wr)
        se_p = math.sqrt(1 / sum(wr))
        lo, hi = pooled - 1.96 * se_p, pooled + 1.96 * se_p
        verdict = {"good": "works", "bad": "hurts", "unclear": "inconclusive"}[outcome_call(items[0][5], (lo, hi))]
        shrunk = []
        for (did, yi, sei, _, quad, _m) in items:
            b = tau2 / (tau2 + sei ** 2) if tau2 > 0 else 0.0
            # "bad luck" is only a fair label when the policy itself is sound; otherwise the policy is the problem
            if quad == "bad_luck" and verdict != "works":
                quad = "policy_problem"
            shrunk.append({"id": did, "raw": round(yi, 3), "shrunk": round(pooled + b * (yi - pooled), 3), "quadrant": quad})
        out[cls] = {"n": len(items), "pooled_effect": round(pooled, 3), "ci95": [round(lo, 3), round(hi, 3)],
                    "expected": items[0][3], "verdict": verdict, "tau": round(math.sqrt(tau2), 3), "instances": shrunk}
    return out
