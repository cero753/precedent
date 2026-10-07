import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "ledger.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, client_id TEXT, channel TEXT, author TEXT, author_role TEXT,
  ts TEXT, thread_id TEXT, campaign_id TEXT, text TEXT
);
CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, client_id TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS embeddings (id TEXT PRIMARY KEY, client_id TEXT, owner_id TEXT, vector TEXT);
CREATE TABLE IF NOT EXISTS edges (
  id TEXT PRIMARY KEY, client_id TEXT, src TEXT, rel TEXT, dst TEXT
);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, client_id TEXT, campaign_id TEXT, action TEXT, decided_by TEXT,
  valid_from TEXT, recorded_at TEXT,
  stated_reason TEXT, evidence TEXT, scope TEXT, scope_confidence REAL,
  memory_class TEXT, trigger_condition TEXT, hypothesis TEXT,
  outcome TEXT, causal_conclusion TEXT, status TEXT, extractor TEXT
);
CREATE TABLE IF NOT EXISTS preferences (
  id TEXT PRIMARY KEY, client_id TEXT, scope TEXT, rule TEXT, kind TEXT,
  status TEXT, confirmed_by TEXT, valid_from TEXT, expires TEXT, supersedes TEXT, compiled TEXT
);
CREATE TABLE IF NOT EXISTS clarifications (
  id TEXT PRIMARY KEY, client_id TEXT, decision_id TEXT, question TEXT, answer TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS lineage (child_id TEXT, child_table TEXT, parent_id TEXT);
CREATE TABLE IF NOT EXISTS facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT, campaign_id TEXT, metric TEXT,
  value REAL, source TEXT, definition_version TEXT, valid_date TEXT,
  recorded_at TEXT, superseded_at TEXT
);
CREATE TABLE IF NOT EXISTS metric_registry (
  metric TEXT, definition_version TEXT, description TEXT, preferred_source TEXT,
  tolerance REAL, effective_from TEXT, PRIMARY KEY (metric, definition_version)
);
CREATE TABLE IF NOT EXISTS context_versions (
  id TEXT PRIMARY KEY, client_id TEXT, key TEXT, value TEXT, valid_from TEXT,
  recorded_at TEXT, supersedes TEXT
);
CREATE TABLE IF NOT EXISTS campaign_state (
  campaign_id TEXT PRIMARY KEY, client_id TEXT, name TEXT, channel TEXT,
  status TEXT, daily_budget REAL
);
CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY, client_id TEXT, campaign_id TEXT, kind TEXT, params TEXT,
  tier TEXT, status TEXT, proposed_by TEXT, rationale TEXT, confidence REAL,
  evidence TEXT, escalation_reasons TEXT, compensating TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT, action_id TEXT, approver TEXT, verdict TEXT,
  raw_text TEXT, parsed TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, client_id TEXT, actor TEXT, event TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS agent_runs (
  id TEXT PRIMARY KEY, client_id TEXT, task TEXT, mode TEXT, model TEXT, trace TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS people (
  person TEXT PRIMARY KEY, client_id TEXT, role TEXT
);
"""

DERIVED_TABLES = ["chunks", "embeddings", "edges", "decisions", "preferences", "clarifications"]


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def connect(path=DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def reset(path=DEFAULT_DB) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).unlink(missing_ok=True)
    return connect(path)


def rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def audit(conn, actor, event, detail=None, client_id=None):
    """Audit rows record who did what to which id. Never message text: content lives only in lineage-tracked tables."""
    conn.execute(
        "INSERT INTO audit (ts, client_id, actor, event, detail) VALUES (?,?,?,?,?)",
        (now(), client_id, actor, event, json.dumps(detail or {})),
    )


def norm_person(name: str) -> str:
    return " ".join((name or "").split()).lower()


def person(conn, name):
    r = conn.execute("SELECT * FROM people WHERE person=?", (norm_person(name),)).fetchone()
    return dict(r) if r else None


def link(conn, child_id, child_table, parent_ids):
    for p in parent_ids:
        conn.execute(
            "INSERT INTO lineage (child_id, child_table, parent_id) VALUES (?,?,?)",
            (child_id, child_table, p),
        )
