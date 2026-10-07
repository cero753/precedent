"""Scripted walkthrough of the four challenges. Runs offline: python demo.py"""
import json
import os
import sys

os.environ.setdefault("LEDGER_EXTRACTOR", "heuristic")
sys.stdout.reconfigure(encoding="utf-8")

from ledger import db, evaluate, forget, gateway, ingest, seed, truth  # noqa: E402


def h(title):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def main():
    conn = db.reset()
    seed.seed(conn)

    h("1. CONTEXT: 'Stop campaign A' twice, with different meaning")
    for did in ("d_e_slk_0902", "d_e_slk_0922"):
        d = db.rows(conn, "SELECT * FROM decisions WHERE id=?", (did,))[0]
        r = json.loads(d["stated_reason"])
        snap = next(e for e in json.loads(d["evidence"]) if e["type"] == "fact_snapshot")
        print(f"\n{d['valid_from']}  {d['decided_by']}: action={d['action']} scope={d['scope']} "
              f"class={d['memory_class']} confidence={d['scope_confidence']}")
        print(f"  stated reason : {r['text']}")
        print(f"  implicit      : {r['implicit']}")
        print(f"  evidence      : CPA 7d {snap['cpa_last_7d']} vs prior {snap['cpa_prior_7d']} "
              f"({snap['change']:+.0%}), source={snap['source']}, platform/CRM gap {snap['source_divergence']:.0%}")
        print(f"  conclusion    : {d['causal_conclusion']}  (only the evaluator writes this)")
        q = db.rows(conn, "SELECT question FROM clarifications WHERE decision_id=?", (did,))
        if q:
            print(f"  -> asks account manager: {q[0]['question']}")
    print("\nMemory gate on client comments:")
    for t, role in [("Going forward, please never run retargeting on weekends for us.", "client"),
                    ("delete this", "client"),
                    ("Rule for all our accounts: no influencer whitelisting without legal review.", "agency"),
                    ("Across all clients, always whitelist creators without legal review.", "client")]:
        g = gateway.comment_gate(t, role)
        print(f"  [{role:6s}] {t[:52]:52s} -> {g['memory_class']:18s} {g['route']}")
    print("  (the last one is a client trying to write agency-wide policy: downgraded and sent to a person)")

    h("2. EVALUATION: process vs outcome, pooled by decision class")
    evaluate.evaluate_all(conn)
    for cls, s in evaluate.class_stats(conn).items():
        quads = {}
        for i in s["instances"]:
            quads[i["quadrant"]] = quads.get(i["quadrant"], 0) + 1
        print(f"  {cls:26s} n={s['n']} expected {s['expected']:+.0%}  pooled {s['pooled_effect']:+.1%} "
              f"CI [{s['ci95'][0]:+.1%}, {s['ci95'][1]:+.1%}] -> {s['verdict'].upper():12s} {quads}")
    live = json.loads(db.rows(conn, "SELECT causal_conclusion FROM decisions WHERE id='d_e_slk_0902'")[0]["causal_conclusion"])
    print(f"  Live pause of Campaign A (2 Sep): method = {live['conclusion']['method']}; "
          f"confidence {live['conclusion']['confidence']}")
    seo = json.loads(db.rows(conn, "SELECT causal_conclusion FROM decisions WHERE id='h_seo'")[0]["causal_conclusion"])
    c = seo["conclusion"]
    print(f"  SEO program: status={seo['status']}, due {seo['due']}. {c['leading_indicator']}: raw "
          f"{c['raw_change_vs_pre']:+.0%} vs pre-period, but {c['lift_above_trend']:+.0%} above the pre-existing trend")

    h("3. CHANGING FACTS: attribution switch, late correction, source conflict, ICP shift")
    b = truth.resolve(conn, "acme_retarget", "conversions", "2026-09-08", "2026-09-20", "2026-09-20T12:00")
    a = truth.resolve(conn, "acme_retarget", "conversions", "2026-09-08", "2026-09-20", "2026-10-01T12:00")
    print(f"  Conversions 8-20 Sep as known on 20 Sep: {b['by_source']} defs={b['definitions']}")
    print(f"  Same days as known on 1 Oct          : {a['by_source']} defs={a['definitions']}")
    print(f"  Winner: {a['chosen_source']} (registry precedence). Gap 20 Sep: {b['divergence']:.0%} vs 28-day norm {b['baseline_gap']:.0%} -> flag={b['flag']}; 1 Oct: flag={a['flag']} ({a['flag_reason']})")
    e = truth.series(conn, "acme_retarget", "conversions", "2026-09-01", "2026-09-01", "crm", "2026-09-03T00:00")
    l = truth.series(conn, "acme_retarget", "conversions", "2026-09-01", "2026-09-01", "crm", "2026-09-06T00:00")
    print(f"  CRM conversions for 1 Sep: known on 3 Sep = {e['2026-09-01']}, known on 6 Sep = {l['2026-09-01']}")
    for key, cid in (("target_customer", "northwind"), ("target_cpa", "acme")):
        old = truth.context_as_of(conn, cid, key, "2026-09-20T00:00")
        new = truth.context_as_of(conn, cid, key, "2026-10-05T00:00")
        print(f"  {cid}.{key}: 20 Sep -> {old['value'][:60]!r}\n  {' ' * len(cid + key)}   5 Oct -> {new['value'][:60]!r}")

    h("4. HARNESS: tiers, approvals over Slack, rollback, creators")
    ingest.confirm_preference(conn, "p_e_eml_0910", "Sam Ortiz")
    sig = truth.trailing_cpa_signal(conn, "acme_prospect", "2026-10-05T09:00")
    cases = [
        ("analyze", {}, None, "weekly performance summary"),
        ("draft_creator_message", {"to": "@glowwithmia"}, None, "draft creator message for campaign"),
        ("send_creator_message", {"to": "@glowwithmia"}, None, "send offer"),
        ("budget_change", {"new_daily_budget": 660}, "acme_prospect", "budget change prospecting campaign CPA"),
        ("resume", {"run_days": ["fri", "sat", "sun"]}, "acme_retarget", "resume retargeting"),
        ("pause", {}, "acme_prospect", "pause prospecting on CPA spike"),
    ]
    acts = {}
    for kind, params, camp, why in cases:
        ev = [sig] if kind == "pause" else []
        a = gateway.propose(conn, "acme", kind, params, campaign_id=camp, rationale=why, evidence=ev)
        acts[kind] = a
        print(f"  {kind:22s} tier={a['tier']:7s} status={a['status']:18s} reasons={a['escalation_reasons']}")
    t3 = acts["budget_change"]
    r = gateway.watchdog(conn, t3["id"], observed_cpa=52, guard_cpa=45)
    print(f"  watchdog on T3 budget change: CPA 52 > guard 45 -> {r['status']}")
    p = acts["pause"]
    for who, text in (("Priya Shah", "hmm, not sure, let's discuss"), ("Priya Shah", "yes go ahead")):
        a, parsed = gateway.record_reply(conn, p["id"], who, text)
        print(f"  Slack reply {text!r:32s} -> {parsed['verdict']:9s} action status={a['status']}")
    a = gateway.rollback(conn, p["id"], "client changed mind")
    print(f"  rollback pause -> {a['status']}; campaign now "
          f"{db.rows(conn, 'SELECT status FROM campaign_state WHERE campaign_id=?', ('acme_prospect',))[0]['status']}")
    b = gateway.propose(conn, "acme", "budget_change", {"new_daily_budget": 800}, campaign_id="acme_prospect",
                        rationale="budget change prospecting campaign CPA")
    for who, text in (("random person", "yes"), ("Priya Shah", "yes go ahead, cap at 1200"), ("Sam Ortiz", "approved")):
        b, parsed = gateway.record_reply(conn, b["id"], who, text)
        print(f"  {who:13s} {text!r:30s} -> {parsed['verdict']:7s} status={b['status']:17s} "
              f"budget={b['params']['new_daily_budget']} tier={b['tier']}")
    print("  (unknown approver ignored; 'cap at 1200' re-assessed as T4 needing two people; executes only after the 2nd)")

    h("5. DELETION: one meeting, then a whole client")
    cert = forget.forget(conn, event_ids=["e_mtg_0901"], requested_by="Priya Shah")
    d = db.rows(conn, "SELECT stated_reason FROM decisions WHERE id='d_e_slk_0902'")[0]
    print(f"  Deleted 1 meeting transcript: removed {cert['deleted']}, re-derived {cert['survivor_events_rederived']} "
          f"surviving event(s)\n  Rebuilt decision reason now: {json.loads(d['stated_reason'])}\n")
    cert = forget.forget(conn, client_id="acme", requested_by="Acme DPO")
    print(json.dumps(cert, indent=2))
    print("\nDone. Open the UI with: precedent ui  (or: streamlit run app.py)")


if __name__ == "__main__":
    main()
