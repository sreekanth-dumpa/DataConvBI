from datetime import timedelta

from conftest import DEMO_Q, confirmed

Q_PV = "30-day quote conversion for NC personal auto quotes in August 2026 by product version, as of 2026-09-15"


def test_exact_and_derived_reuse_with_weighted_rollup(svc):
    first = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    again = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    assert again["origin"] == "cache_exact"
    rolled = svc.ask(Q_PV, {"subject": "analyst_nc"})
    assert rolled["origin"] == "cache_derived" and rolled["receipt"]["rule"] == "rw.derived_rollup_sum_v1"
    fine = first["facts"]["by_dimension"]
    for r in rolled["facts"]["by_dimension"]:
        parts = [x for x in fine if x["product_version"] == r["product_version"]]
        conv, mat = sum(p["converted"] for p in parts), sum(p["eligible_mature"] for p in parts)
        assert r["conversion_rate"] == conv / mat  # recomputed from sums, never averaged rates
    assert rolled["receipt"]["producer_execution_id"] == first["execution_id"]


def test_incompatible_requests_are_rejected(svc):
    svc.ask(Q_PV, {"subject": "analyst_nc"})
    finer = svc.ask(DEMO_Q, {"subject": "analyst_nc"})  # new dimension: cannot be derived
    assert finer["origin"] == "fresh_execution"
    later = svc.ask(Q_PV.replace("as of 2026-09-15", "as of 2026-09-14"), {"subject": "analyst_nc"})
    assert later["origin"] == "fresh_execution"
    other_window = svc.ask(Q_PV.replace("30-day", "45-day"), {"subject": "analyst_nc"})
    assert other_window["origin"] == "fresh_execution"
    legacy = svc.ask("legacy " + Q_PV, {"subject": "analyst_nc"})
    assert legacy["origin"] == "fresh_execution"


def test_subject_scoped_reuse_without_access_certificate(svc):
    svc.ask(Q_PV, {"subject": "analyst_nc"})
    other = svc.ask(Q_PV, {"subject": "analyst_nc_2"})  # same visibility, but no certified class
    assert other["origin"] == "fresh_execution"


def test_certified_access_class_permits_shared_reuse(make_service):
    svc = make_service(certify_access_classes=True)
    svc.ask(Q_PV, {"subject": "analyst_nc"})
    other = svc.ask(Q_PV, {"subject": "analyst_nc_2"})
    assert other["origin"] == "cache_exact"
    wider = svc.ask(Q_PV, {"subject": "analyst_south"})  # different row visibility -> different class
    assert wider["origin"] == "fresh_execution"


def test_policy_change_invalidates_and_reauthorization(svc):
    svc.ask(Q_PV, {"subject": "analyst_nc"})
    svc.set_policy_mapping("analyst_nc", ["NC", "SC"])  # entitlement change -> access fingerprint changes
    again = svc.ask(Q_PV, {"subject": "analyst_nc"})
    assert again["origin"] == "fresh_execution"
    n = svc.bump_policy_version("rap_state_by_subject_v4")
    assert n >= 1


def test_source_change_selects_new_cut_and_rejects_old_entry(svc):
    first = svc.ask(Q_PV, {"subject": "analyst_nc"})
    svc.clock.advance(timedelta(days=4, hours=1))  # publication seq 3 becomes available
    rid = confirmed(svc, Q_PV)
    p = svc.prepare(rid)
    assert p["cut"].entries[0].seq == 3
    assert p["cache"].outcome == "miss"
    assert any("source cut" in r["reason"] for r in p["cache"].rejected)
    assert first["origin"] == "fresh_execution"


def test_failed_runs_are_never_cached(svc):
    svc.engine.faults["fail"] = ['"node":"fused_metric"', '"node":"metric_stats"', '"node":"merge"']
    svc.runtime.max_attempts = 1
    out = svc.ask(Q_PV, {"subject": "analyst_nc"})
    assert out["state"] == "failed"
    assert not svc.cache.entries()
