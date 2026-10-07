"""Right-to-erasure by lineage walk: delete source events, every descendant, then re-derive survivors."""
from .db import DERIVED_TABLES, audit, rows

LINEAGE_TABLES = DERIVED_TABLES + ["context_versions"]
CLIENT_TABLES = LINEAGE_TABLES + ["events", "facts", "actions", "approvals", "campaign_state", "agent_runs"]


def descendants(conn, event_ids):
    """Every derived row with at least one deleted ancestor (derived content may embed the deleted text)."""
    seen, frontier = {}, set(event_ids)
    while frontier:
        q = ",".join("?" * len(frontier))
        nxt = set()
        for r in rows(conn, f"SELECT child_id, child_table FROM lineage WHERE parent_id IN ({q})", tuple(frontier)):
            if r["child_id"] not in seen:
                seen[r["child_id"]] = r["child_table"]
                nxt.add(r["child_id"])
        frontier = nxt
    return seen


def residual(conn, client_id=None, event_ids=None, deleted_ids=()):
    """Count anything still traceable to the deleted scope. Must be all zeros after forget()."""
    out = {}
    if client_id:
        for t in CLIENT_TABLES:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t} WHERE client_id=?", (client_id,)).fetchone()[0]
        out["audit_rows_with_detail"] = conn.execute(
            "SELECT COUNT(*) FROM audit WHERE client_id=? AND detail!='{\"redacted\": true}'", (client_id,)).fetchone()[0]
    if event_ids:
        q = ",".join("?" * len(event_ids))
        out["events"] = conn.execute(f"SELECT COUNT(*) FROM events WHERE id IN ({q})", tuple(event_ids)).fetchone()[0]
        out["lineage_children"] = conn.execute(f"SELECT COUNT(*) FROM lineage WHERE parent_id IN ({q})", tuple(event_ids)).fetchone()[0]
    if deleted_ids:
        q = ",".join("?" * len(deleted_ids))
        out["dangling_references"] = (
            conn.execute(f"SELECT COUNT(*) FROM context_versions WHERE supersedes IN ({q})", tuple(deleted_ids)).fetchone()[0]
            + conn.execute(f"SELECT COUNT(*) FROM clarifications WHERE decision_id IN ({q})", tuple(deleted_ids)).fetchone()[0]
            + conn.execute(f"SELECT COUNT(*) FROM lineage WHERE child_id IN ({q})", tuple(deleted_ids)).fetchone()[0])
    return out


def forget(conn, client_id=None, event_ids=None, requested_by="client", rederive=True):
    from . import ingest

    if client_id:
        event_ids = [r["id"] for r in rows(conn, "SELECT id FROM events WHERE client_id=?", (client_id,))]
    event_ids = list(event_ids or [])
    desc = descendants(conn, event_ids)

    # survivors: parents of deleted children that are not themselves being deleted -> re-derive from them
    survivors = set()
    if desc:
        q = ",".join("?" * len(desc))
        for r in rows(conn, f"SELECT DISTINCT parent_id FROM lineage WHERE child_id IN ({q})", tuple(desc)):
            if r["parent_id"] not in event_ids and r["parent_id"] not in desc:
                survivors.add(r["parent_id"])

    counts = {}
    for child_id, table in desc.items():
        if table == "context_versions":  # keep the version chain intact: point successors at the predecessor
            prev = conn.execute("SELECT supersedes FROM context_versions WHERE id=?", (child_id,)).fetchone()
            conn.execute("UPDATE context_versions SET supersedes=? WHERE supersedes=?", (prev[0] if prev else None, child_id))
        conn.execute(f"DELETE FROM {table} WHERE id=?", (child_id,))
        counts[table] = counts.get(table, 0) + 1
    ids = list(desc) + event_ids
    if ids:
        q = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM lineage WHERE child_id IN ({q}) OR parent_id IN ({q})", tuple(ids) * 2)
        qe = ",".join("?" * len(event_ids)) or "''"
        conn.execute(f"DELETE FROM events WHERE id IN ({qe})", tuple(event_ids))
    counts["events"] = len(event_ids)
    if client_id:
        for t in ("facts", "approvals", "actions", "campaign_state", "context_versions", "agent_runs", "clarifications"):
            n = conn.execute(f"DELETE FROM {t} WHERE client_id=?", (client_id,)).rowcount
            if n:
                counts[t] = counts.get(t, 0) + n
        # audit keeps that something happened (who, when, which id) but loses all detail for this client
        counts["audit_redacted"] = conn.execute(
            "UPDATE audit SET detail='{\"redacted\": true}' WHERE client_id=?", (client_id,)).rowcount

    rebuilt = 0
    if rederive and survivors:
        q = ",".join("?" * len(survivors))
        survivor_events = rows(conn, f"SELECT * FROM events WHERE id IN ({q}) ORDER BY ts", tuple(survivors))
        for ev in survivor_events:  # wipe and rebuild their derived rows so nothing half-derived remains
            for d_id, t in descendants(conn, [ev["id"]]).items():
                conn.execute(f"DELETE FROM {t} WHERE id=?", (d_id,))
            conn.execute("DELETE FROM lineage WHERE parent_id=?", (ev["id"],))
        for ev in survivor_events:
            ingest.ingest_event(conn, ev)
            rebuilt += 1

    certificate = {"requested_by": requested_by, "client_id": client_id, "source_events": len(event_ids),
                   "deleted": counts, "survivor_events_rederived": rebuilt,
                   "residual": residual(conn, client_id, event_ids, [
                       i for i, t in desc.items() if not conn.execute(f"SELECT 1 FROM {t} WHERE id=?", (i,)).fetchone()]),
                   "not_covered_here": ["backups and logs: per-client key destruction (crypto-shredding)",
                                        "LLM provider: zero-data-retention agreement, nothing stored provider-side",
                                        "pooled decision-class statistics: computed on read, so they drop deleted data automatically"]}
    audit(conn, "privacy", "erasure_completed", {"client_id": client_id, "events": len(event_ids)})
    conn.commit()
    return certificate
