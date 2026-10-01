"""Independent hand labels vs. the independent oracle vs. the compiled SQL."""
from ipr.engines.base import udf_add_local_days
from ipr.fixture import DEMO_PARAMS, EDGE_LABELS, EDGE_LEGACY_COHORT
from ipr.oracle import oracle
from ipr.sqlgen import Dialect, cohort_sql, linked_binds_sql, outcome_sql


def _oracle(svc, identity="submission", tz="America/New_York"):
    params = dict(DEMO_PARAMS, dimensions=["product_version", "channel"])
    return oracle(str(svc.settings.data_db), params, identity, tz, 2, 2, {"NC"})


def test_oracle_matches_hand_labels(svc):
    units = _oracle(svc)["units"]
    for sub, label in EDGE_LABELS.items():
        if label["class"] in ("not_in_cohort", "not_in_scope"):
            assert sub not in units, sub
        else:
            got = units[sub]
            assert got["class"] == label["class"], (sub, got)
            assert (got["product_version"], got["channel"]) == (label["product_version"], label["channel"]), sub


def test_compiled_sql_matches_hand_labels(svc):
    d = Dialect("sqlite")
    sql = ("WITH cohort AS (" + cohort_sql(d, "submission", False) + "), linked AS (" +
           linked_binds_sql(d, "submission", "cohort", False) + "), outcome AS (" + outcome_sql("cohort", "linked") +
           ") SELECT opportunity_id, product_version, channel, cohort_class, converted FROM outcome WHERE opportunity_id LIKE 'EDGE-%'")
    params = {"state": "NC", "lob": "personal_auto", "cohort_start": "2026-08-01", "cohort_end": "2026-09-01", "window_days": 30,
              "cutoff": DEMO_PARAMS["knowledge_cutoff"], "tz": "America/New_York"}
    sess = svc.engine.acquire("analyst_nc", "pricing_review")
    try:
        rows = {r["opportunity_id"]: r for r in sess.fetch_all(sql, params, cut={"quote_fdp/quoted_opportunities_v2": 2,
                                                                                  "policy_fdp/known_bind_events_v2": 2})}
    finally:
        svc.engine.release(sess)
    for sub, label in EDGE_LABELS.items():
        if label["class"] in ("not_in_cohort", "not_in_scope"):
            assert sub not in rows
            continue
        r = rows[sub]
        cls = r["cohort_class"] if r["cohort_class"] != "mature" else ("mature_converted" if r["converted"] else "mature_not_converted")
        assert cls == label["class"], (sub, r)


def test_legacy_utc_cohort_differences(svc):
    units = _oracle(svc, identity="branch", tz="UTC")["units"]
    for sub, in_cohort in EDGE_LEGACY_COHORT.items():
        assert (f"{sub}-B1" in units) == in_cohort


def test_dst_window_end_keeps_local_wall_time():
    # 2026-10-15 12:00 EDT (16:00Z) + 30 local days = 2026-11-14 12:00 EST (17:00Z)
    assert udf_add_local_days("2026-10-15T16:00:00Z", "America/New_York", 30) == "2026-11-14T17:00:00Z"
