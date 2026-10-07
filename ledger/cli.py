"""precedent command line.

  precedent demo                      scripted walkthrough on the synthetic agency
  precedent ui                        open the Streamlit app
  precedent eval [--backend llm]      run the eval harness
  precedent ingest events.jsonl       extract decisions from your own messages
"""
import argparse
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ["id", "client_id", "channel", "author", "author_role", "ts", "text"]


def ingest_file(path, db_path, people=None):
    from . import db, ingest

    conn = db.connect(db_path)
    rows, bad = [], []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        ev = json.loads(line)
        missing = [k for k in REQUIRED if not ev.get(k)]
        if missing:
            bad.append(f"line {n}: missing {missing}")
            continue
        rows.append((ev["id"], ev["client_id"], ev["channel"], ev["author"], ev["author_role"], ev["ts"][:16],
                     ev.get("thread_id") or ev["id"], ev.get("campaign_id"), ev["text"]))
    if bad:
        raise SystemExit("Invalid events:\n  " + "\n  ".join(bad))
    conn.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?)", rows)
    for p in people or []:
        conn.execute("INSERT OR REPLACE INTO people VALUES (?,?,?)", (db.norm_person(p["person"]), p["client_id"], p["role"]))
    ingest.ingest_all(conn)
    return conn


def report(conn):
    from .db import rows

    print(f"\n{'when':17s} {'who':16s} {'action':16s} {'class':19s} {'scope':9s} conf  reason")
    for d in rows(conn, "SELECT * FROM decisions WHERE extractor!='import' ORDER BY valid_from"):
        r = json.loads(d["stated_reason"])
        print(f"{d['valid_from'][:16]:17s} {d['decided_by'][:16]:16s} {d['action'][:16]:16s} {d['memory_class']:19s} "
              f"{d['scope']:9s} {d['scope_confidence']:.2f}  {(r.get('text') or '-')[:60]}")
    qs = rows(conn, "SELECT question FROM clarifications WHERE status='open'")
    if qs:
        print("\nQuestions for a person:")
        for q in qs:
            print("  -", q["question"])
    prefs = rows(conn, "SELECT id, kind, client_id, compiled, rule FROM preferences WHERE status='proposed'")
    if prefs:
        print("\nProposed lasting preferences (need confirmation before they bind):")
        for p in prefs:
            print(f"  - [{p['kind']} · {p['client_id']}] {p['rule'][:70]}  ->  enforced as {p['compiled']}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="precedent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo")
    sub.add_parser("ui")
    e = sub.add_parser("eval")
    e.add_argument("--backend", default="heuristic", choices=["heuristic", "llm"])
    g = sub.add_parser("agent", help="run the planner agent on the synthetic agency")
    g.add_argument("task", nargs="?", default="Weekly optimisation review for Acme Skin. Also prepare outreach for "
                                                "creator @glowwithmia for the October launch.")
    g.add_argument("--client", default="acme")
    g.add_argument("--mode", choices=["live", "replay", "offline"], default=None,
                   help="live needs LLM_API_KEY, LLM_MODEL and LLM_BASE_URL in .env; default: live if configured, else offline")
    g.add_argument("--record", action="store_true", help="save a live run as examples/agent_run_recorded.json for offline replay")
    i = sub.add_parser("ingest")
    i.add_argument("events", help="JSON Lines file, one message per line")
    i.add_argument("--db", default="ledger.db")
    i.add_argument("--people", help="optional JSON list of approvers: [{person, client_id, role}]")
    a = ap.parse_args(argv)
    for k in ("events", "db", "people"):
        if getattr(a, k, None):
            setattr(a, k, str(Path(getattr(a, k)).resolve()))

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    if a.cmd == "demo":
        runpy.run_path(str(ROOT / "demo.py"), run_name="__main__")
    elif a.cmd == "ui":
        subprocess.run([sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py")], check=False)
    elif a.cmd == "eval":
        from evals import run_evals
        run_evals.print_report(run_evals.run(a.backend))
    elif a.cmd == "agent":
        from . import agent, db, evaluate, llm, seed

        mode = a.mode or ("live" if llm.provider() else "offline")
        conn = db.reset(":memory:")
        seed.seed(conn)
        evaluate.evaluate_all(conn)
        print(f"Agent run: mode={mode}" + (f", model={llm.model_name()}" if mode == "live" else ""))
        r = agent.run(conn, a.client, a.task, mode=mode)
        for n, s in enumerate(r["steps"], 1):
            if s["thought"]:
                print(f"\n[{n}] {s['thought'].strip()[:400]}")
            for c in s["calls"]:
                res = c["result"]
                verdict = f"  => {res['tier']} {res['status']} {res.get('gateway_reasons') or ''}" if isinstance(res, dict) and "tier" in res else ""
                print(f"    -> {c['tool']}({json.dumps(c['args'])[:160]}){verdict}")
        print(f"\nSummary: {r['summary']}\nTokens: {r['usage']}  invalid tool calls: {r['invalid_tool_calls']}")
        if a.record and mode == "live":
            agent.save_recording(r)
            print(f"Recorded to {agent.RECORDED}")
    elif a.cmd == "ingest":
        people = json.loads(Path(a.people).read_text(encoding="utf-8")) if a.people else None
        report(ingest_file(a.events, a.db, people))


if __name__ == "__main__":
    main()
