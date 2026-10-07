"""Bitemporal fact queries, metric-definition versions, source precedence and divergence flags."""
from datetime import date, timedelta

FAR_FUTURE = "9999-12-31T00:00:00"


def _norm(ts):
    if ts is None:
        return FAR_FUTURE
    return ts + ":00" if len(ts) == 16 else (ts + "T23:59:59" if len(ts) == 10 else ts)


def current_definition(conn, metric, source, known_at=None):
    """Definition version in force at `known_at` for a given source (e.g. click_7d before the switch)."""
    known_at = _norm(known_at)
    row = conn.execute(
        "SELECT r.definition_version FROM metric_registry r WHERE r.metric=? AND r.effective_from<=?"
        " AND EXISTS (SELECT 1 FROM facts f WHERE f.metric=r.metric AND f.source=? AND f.definition_version=r.definition_version)"
        " ORDER BY r.effective_from DESC LIMIT 1",
        (metric, known_at[:10], source),
    ).fetchone()
    return row[0] if row else None


def series(conn, campaign_id, metric, start, end, source, known_at=None, definition=None):
    """Daily values as they were known at `known_at` (recorded before it, not yet superseded)."""
    known_at = _norm(known_at)
    definition = definition or current_definition(conn, metric, source, known_at)
    out = {}
    for r in conn.execute(
        "SELECT valid_date, value FROM facts WHERE campaign_id=? AND metric=? AND source=?"
        " AND definition_version=? AND valid_date BETWEEN ? AND ? AND recorded_at<=?"
        " AND (superseded_at IS NULL OR superseded_at>?) ORDER BY valid_date",
        (campaign_id, metric, source, definition, start, end, known_at, known_at),
    ):
        out[r[0]] = out.get(r[0], 0) + r[1]
    return out


def sources_for(conn, campaign_id, metric):
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM facts WHERE campaign_id=? AND metric=?", (campaign_id, metric))]


def resolve(conn, campaign_id, metric, start, end, known_at=None):
    """Totals per source, the precedence winner, and a divergence flag. Nothing is overwritten."""
    by_source, defs = {}, {}
    for src in sources_for(conn, campaign_id, metric):
        d = current_definition(conn, metric, src, known_at)
        defs[src] = d
        by_source[src] = round(sum(series(conn, campaign_id, metric, start, end, src, known_at, d).values()), 2)
    reg = conn.execute(
        "SELECT preferred_source, tolerance FROM metric_registry WHERE metric=? LIMIT 1", (metric,)).fetchone()
    preferred = reg["preferred_source"] if reg and reg["preferred_source"] in by_source else next(iter(by_source), None)
    value = by_source.get(preferred)
    tol = reg["tolerance"] if reg else 0.1
    divergence, flag, baseline_gap, shift = 0.0, False, None, None
    if len(by_source) > 1 and value:
        others = [s for s in by_source if s != preferred]
        divergence = max(abs(by_source[s] - value) / value for s in others)
        # Platforms over-report vs CRM by a fairly stable amount. Flag when the gap MOVES, not merely exists.
        s0 = date.fromisoformat(start)
        base = [_gap(conn, campaign_id, metric, (s0 - timedelta(days=28)).isoformat(), (s0 - timedelta(days=1)).isoformat(),
                     known_at, preferred, s, defs) for s in others]
        cur = [_gap(conn, campaign_id, metric, start, end, known_at, preferred, s, defs) for s in others]
        if all(b is not None for b in base) and all(c is not None for c in cur):
            baseline_gap = round(max(base), 3)
            shift = round(max(abs((1 + c) / (1 + b) - 1) for c, b in zip(cur, base)), 3)
            flag = shift > tol
            reason = f"gap moved {shift:.0%} from its 28-day norm" if flag else None
        else:
            flag = divergence > tol
            reason = "no 28-day baseline under the current definitions (definition changed?)" if flag else None
    else:
        reason = None
    return {"metric": metric, "by_source": by_source, "definitions": defs, "chosen_source": preferred,
            "value": value, "divergence": round(divergence, 3), "baseline_gap": baseline_gap,
            "gap_shift": shift, "flag": flag, "flag_reason": reason}


def _gap(conn, campaign_id, metric, start, end, known_at, preferred, other, defs):
    """Relative gap other/preferred over days both sources report, under the definitions in force at known_at."""
    p = series(conn, campaign_id, metric, start, end, preferred, known_at, defs[preferred])
    o = series(conn, campaign_id, metric, start, end, other, known_at, defs[other])
    days = [d for d in p if d in o and p[d] > 0]
    if len(days) < 5:
        return None
    return sum(o[d] for d in days) / sum(p[d] for d in days) - 1


def cpa(conn, campaign_id, start, end, known_at=None):
    spend = resolve(conn, campaign_id, "spend", start, end, known_at)
    conv = resolve(conn, campaign_id, "conversions", start, end, known_at)
    per_source = {s: round(spend["value"] / c, 2) if c else None for s, c in conv["by_source"].items()}
    chosen = per_source.get(conv["chosen_source"])
    return {"cpa": chosen, "cpa_by_source": per_source, "spend": spend["value"], "conversions": conv}


def trailing_cpa_signal(conn, campaign_id, as_of_ts):
    """CPA in the 7 days before a decision vs the 7 days before that, using only what was known then."""
    d = date.fromisoformat(as_of_ts[:10])
    cur = cpa(conn, campaign_id, (d - timedelta(days=7)).isoformat(), (d - timedelta(days=1)).isoformat(), as_of_ts)
    prev = cpa(conn, campaign_id, (d - timedelta(days=14)).isoformat(), (d - timedelta(days=8)).isoformat(), as_of_ts)
    change = None
    if cur["cpa"] and prev["cpa"]:
        change = round(cur["cpa"] / prev["cpa"] - 1, 3)
    owner = conn.execute("SELECT client_id FROM campaign_state WHERE campaign_id=?", (campaign_id,)).fetchone()
    target = context_as_of(conn, owner[0], "target_cpa", as_of_ts) if owner else None
    return {"campaign_id": campaign_id, "as_of": as_of_ts, "cpa_last_7d": cur["cpa"], "cpa_prior_7d": prev["cpa"],
            "change": change, "source": cur["conversions"]["chosen_source"],
            "definitions": cur["conversions"]["definitions"],
            "target_cpa": float(target["value"]) if target else None, "target_version": target["id"] if target else None,
            "source_divergence": cur["conversions"]["divergence"], "gap_shift": cur["conversions"]["gap_shift"],
            "divergence_flag": cur["conversions"]["flag"]}


def context_as_of(conn, client_id, key, known_at=None):
    known_at = _norm(known_at)
    r = conn.execute(
        "SELECT * FROM context_versions WHERE client_id=? AND key=? AND recorded_at<=? AND valid_from<=?"
        " ORDER BY recorded_at DESC LIMIT 1", (client_id, key, known_at, known_at[:10])).fetchone()
    return dict(r) if r else None
