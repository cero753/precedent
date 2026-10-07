"""Synthetic agency world: 2 clients, 4 live campaigns, conversations, metric feeds, past decisions."""
import json
import random
from datetime import date, timedelta

CLIENTS = {
    "acme": "Acme Skin (DTC skincare)",
    "northwind": "Northwind (B2B SaaS)",
}

CAMPAIGNS = [
    ("acme_retarget", "acme", "Campaign A - Retargeting", "meta", "paused", 400.0),
    ("acme_prospect", "acme", "Campaign B - Prospecting", "meta", "active", 600.0),
    ("nw_search", "northwind", "Search - Pricing intent", "google", "paused", 300.0),
    ("nw_seo", "northwind", "SEO comparison pages", "organic", "active", 0.0),
]

# Approver allowlist: (normalised name, client scope, role). '*' = agency staff, all clients.
PEOPLE = [
    ("priya shah", "acme", "client_approver"),
    ("dana kim", "northwind", "client_approver"),
    ("sam ortiz", "*", "agency_lead"),
    ("lena ruiz", "*", "account_manager"),
]

EVENTS = [
    ("e_brief_nw", "northwind", "email", "Dana Kim", "client", "2026-08-01T10:00", "t_onb", None,
     "Onboarding brief: our target customer is SMBs with 10-200 employees. Primary goal is demo requests."),
    ("e_mtg_0901", "acme", "meeting", "Priya Shah", "client", "2026-09-01T15:00", "t_a1", "acme_retarget",
     "Weekly sync transcript. Priya: the new retargeting creative feels off-brand, our founder hates the discount tone. "
     "Sam: noted, CPA on Campaign A has also crept up this week."),
    ("e_slk_0902", "acme", "slack", "Priya Shah", "client", "2026-09-02T09:12", "t_a1", "acme_retarget",
     "Stop campaign A for now. The creative feels off-brand."),
    ("e_slk_0908", "acme", "slack", "Sam Ortiz", "agency", "2026-09-08T11:00", "t_a2", "acme_retarget",
     "Campaign A relaunched with the new brand-approved creative."),
    ("e_eml_0910", "acme", "email", "Priya Shah", "client", "2026-09-10T08:30", "t_a3", "acme_retarget",
     "Going forward, please never run retargeting on weekends for us. Weekend traffic converts terribly for our category."),
    ("e_slk_0915", "northwind", "slack", "Dana Kim", "client", "2026-09-15T14:05", "t_n1", "nw_search",
     "Pause search until the pricing page is fixed."),
    ("e_slk_0916", "northwind", "slack", "Dana Kim", "client", "2026-09-16T10:20", "t_n2", None,
     "delete this"),
    ("e_slk_0918", "agency", "slack", "Sam Ortiz", "agency", "2026-09-18T16:40", "t_g1", None,
     "Rule for all our accounts: no influencer whitelisting without legal review."),
    ("e_slk_0922", "acme", "slack", "Priya Shah", "client", "2026-09-22T17:55", "t_a4", "acme_retarget",
     "Stop campaign A."),
    ("e_mtg_0929", "northwind", "meeting", "Dana Kim", "client", "2026-09-29T16:00", "t_n3", None,
     "Quarterly review transcript. Dana: we are moving upmarket. From now on our target is companies with 200+ employees, not SMBs."),
]

START, END = date(2026, 8, 18), date(2026, 10, 5)
ATTRIBUTION_SWITCH = date(2026, 9, 21)


def _days():
    d = START
    while d <= END:
        yield d
        d += timedelta(days=1)


def _a_running(d):
    if date(2026, 9, 2) <= d < date(2026, 9, 8):
        return False
    return d < date(2026, 9, 22)


def seed_facts(conn, rng):
    """Daily metric feeds. Platform over-reports conversions vs CRM; attribution restated 7d -> 14d."""
    ins = ("INSERT INTO facts (client_id, campaign_id, metric, value, source, definition_version,"
           " valid_date, recorded_at, superseded_at) VALUES (?,?,?,?,?,?,?,?,?)")
    for d in _days():
        ds, rec = d.isoformat(), (d + timedelta(days=1)).isoformat() + "T06:00:00"
        defn = "click_7d" if d < ATTRIBUTION_SWITCH else "click_14d"
        # Campaign A: CPA drifts up into early Sept (the unspoken reason), then stops
        if _a_running(d):
            spend = 400 * rng.uniform(0.9, 1.1)
            ramp = (d - date(2026, 8, 25)).days
            cpa_true = (30 + 1.6 * max(0, ramp)) if d < date(2026, 9, 8) else 34
            crm = max(1, round(spend / cpa_true * rng.uniform(0.85, 1.15)))
            plat = round(crm * rng.uniform(1.25, 1.4))
        else:
            spend, crm, plat = 0.0, 0, 0
        spend_b = 600 * rng.uniform(0.9, 1.1)
        crm_b = round(spend_b / 41 * rng.uniform(0.85, 1.15))
        plat_b = round(crm_b * rng.uniform(1.2, 1.35))
        for cid, sp, pl, cr in (("acme_retarget", spend, plat, crm), ("acme_prospect", spend_b, plat_b, crm_b)):
            conn.execute(ins, ("acme", cid, "spend", round(sp, 2), "ad_platform", "v1", ds, rec, None))
            conn.execute(ins, ("acme", cid, "conversions", pl, "ad_platform", defn, ds, rec, None))
            conn.execute(ins, ("acme", cid, "conversions", cr, "crm", "crm_v1", ds, rec, None))
        # Northwind search + SEO
        nw_on = not (d >= date(2026, 9, 15))
        sp_n = 300 * rng.uniform(0.9, 1.1) if nw_on else 0.0
        cr_n = round(sp_n / 95 * rng.uniform(0.7, 1.3)) if nw_on else 0
        conn.execute(ins, ("northwind", "nw_search", "spend", round(sp_n, 2), "ad_platform", "v1", ds, rec, None))
        conn.execute(ins, ("northwind", "nw_search", "conversions", round(cr_n * 1.5), "ad_platform", defn, ds, rec, None))
        conn.execute(ins, ("northwind", "nw_search", "conversions", cr_n, "crm", "crm_v1", ds, rec, None))
        sessions = 120 + 4 * (d - START).days + rng.randint(-15, 15)
        conn.execute(ins, ("northwind", "nw_seo", "organic_sessions", sessions, "analytics", "v1", ds, rec, None))

    # Platform restates the last 14 days under the new 14-day window (new definition, old rows kept)
    for i in range(14):
        d = ATTRIBUTION_SWITCH - timedelta(days=i + 1)
        old = conn.execute(
            "SELECT * FROM facts WHERE campaign_id='acme_retarget' AND metric='conversions' AND source='ad_platform'"
            " AND valid_date=?", (d.isoformat(),)).fetchone()
        conn.execute(ins, ("acme", "acme_retarget", "conversions", round(old["value"] * 1.12), "ad_platform",
                           "click_14d", d.isoformat(), "2026-09-21T06:00:00", None))

    # Late-arriving CRM correction: 1 Sep first recorded on 2 Sep, corrected on 5 Sep
    row = conn.execute("SELECT id, value FROM facts WHERE campaign_id='acme_retarget' AND metric='conversions'"
                       " AND source='crm' AND valid_date='2026-09-01'").fetchone()
    conn.execute("UPDATE facts SET superseded_at='2026-09-05T06:00:00' WHERE id=?", (row["id"],))
    conn.execute(ins, ("acme", "acme_retarget", "conversions", row["value"] + 2, "crm", "crm_v1",
                       "2026-09-01", "2026-09-05T06:00:00", None))


def seed_registry(conn):
    reg = [
        ("conversions", "click_7d", "Platform conversions, 7-day click attribution", "crm", 0.15, "2026-01-01"),
        ("conversions", "click_14d", "Platform conversions, 14-day click attribution", "crm", 0.15, "2026-09-21"),
        ("conversions", "crm_v1", "CRM-confirmed orders / SQLs, deduplicated", "crm", 0.15, "2026-01-01"),
        ("spend", "v1", "Media spend", "ad_platform", 0.02, "2026-01-01"),
        ("organic_sessions", "v1", "Organic sessions", "analytics", 0.05, "2026-01-01"),
    ]
    conn.executemany("INSERT INTO metric_registry VALUES (?,?,?,?,?,?)", reg)
    ctx = [
        ("ctx_acme_cpa_v1", "acme", "target_cpa", "40", "2026-08-01", "2026-08-01T00:00:00", None),
        ("ctx_acme_cpa_v2", "acme", "target_cpa", "35", "2026-09-25", "2026-09-25T09:00:00", "ctx_acme_cpa_v1"),
    ]
    conn.executemany("INSERT INTO context_versions VALUES (?,?,?,?,?,?,?)", ctx)


# --- historical decisions for the evaluator --------------------------------------------------
CLASSES = {
    # class: (action, true relative effect on CPA, expected effect written at decision time)
    "pause_on_cpa_spike": ("pause", -0.12, -0.15),
    "budget_up_on_strong_roas": ("budget_up", 0.04, -0.05),
    "creative_refresh": ("creative_refresh", -0.06, -0.10),
}


def historical_decisions(rng):
    out, n = [], 0
    for cls, (action, true_eff, expected) in CLASSES.items():
        for _ in range(6):
            n += 1
            base_t, base_c = rng.uniform(30, 50), rng.uniform(30, 50)
            drift = rng.uniform(-0.05, 0.05)  # market-wide move both campaigns feel
            eff = true_eff + rng.gauss(0, 0.06)  # each instance is noisy
            series = lambda base, mult: [round(base * mult * rng.uniform(0.88, 1.12), 2) for _ in range(14)]
            obs = {
                "treated_pre": series(base_t, 1.0), "treated_post": series(base_t, (1 + drift) * (1 + eff)),
                "control_pre": series(base_c, 1.0), "control_post": series(base_c, 1 + drift),
            }
            good_process = rng.random() > 0.3
            process = {
                "evidence_attached": good_process or rng.random() > 0.5,
                "hypothesis_preregistered": good_process,
                "policy_compliant": True if good_process else rng.random() > 0.4,
                "sources_agreed_at_time": good_process or rng.random() > 0.5,
            }
            out.append({
                "id": f"h{n:02d}", "client_id": "acme" if n % 2 else "northwind", "class": cls, "action": action,
                "valid_from": (date(2026, 6, 1) + timedelta(days=4 * n)).isoformat(),
                "hypothesis": {"metric": "cpa", "expected_rel": expected, "horizon_days": 14,
                               "leading": "ctr", "class": cls, "process": process},
                "observations": obs,
            })
    # One delayed-outcome decision: SEO, 120-day horizon, only a leading indicator so far
    out.append({
        "id": "h_seo", "client_id": "northwind", "class": "seo_content_program", "action": "publish_pages",
        "valid_from": "2026-09-20",
        "hypothesis": {"metric": "demo_requests_from_organic", "expected_rel": 0.25, "horizon_days": 120,
                       "leading": "organic_sessions", "class": "seo_content_program",
                       "process": {"evidence_attached": True, "hypothesis_preregistered": True,
                                   "policy_compliant": True, "sources_agreed_at_time": True}},
        "observations": None,
    })
    return out


def seed(conn, seed_value=7):
    from . import ingest
    rng = random.Random(seed_value)
    conn.executemany("INSERT INTO campaign_state VALUES (?,?,?,?,?,?)", CAMPAIGNS)
    conn.executemany("INSERT INTO people VALUES (?,?,?)", PEOPLE)
    seed_registry(conn)
    seed_facts(conn, rng)
    for e in EVENTS:
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", e)
    ingest.ingest_all(conn)
    for h in historical_decisions(rng):
        conn.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            (f"e_hist_{h['id']}", h["client_id"], "ledger_import", "historical", "agency",
             h["valid_from"] + "T09:00", f"t_{h['id']}", None, f"Imported historical decision {h['id']} ({h['class']})"),
        )
        conn.execute(
            "INSERT INTO decisions (id, client_id, action, decided_by, valid_from, recorded_at, stated_reason,"
            " evidence, scope, scope_confidence, memory_class, hypothesis, outcome, status, extractor)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (h["id"], h["client_id"], h["action"], "historical", h["valid_from"], h["valid_from"],
             json.dumps({"text": h["class"]}), "[]", "campaign", 1.0, "one_off", json.dumps(h["hypothesis"]),
             json.dumps({"observations": h["observations"]}), "pending", "import"),
        )
        from .db import link
        link(conn, h["id"], "decisions", [f"e_hist_{h['id']}"])
    conn.commit()
