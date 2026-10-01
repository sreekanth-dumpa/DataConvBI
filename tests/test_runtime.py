import time

from ipr.validation import node_assertions
from conftest import confirmed

Q = "30-day quote conversion for NC personal auto quotes in August 2026 by product version and channel, as of 2026-09-15"


def _planned(svc, subject="analyst_nc"):
    rid = confirmed(svc, Q, subject=subject)
    svc.prepare(rid)
    svc.plan(rid)
    return rid


def test_lost_ack_is_reconciled_by_tag_not_resubmitted(svc):
    rid = _planned(svc)
    svc.engine.faults["lose_ack"] = ['"node":"linked_binds"']
    ex = svc.execute(rid, variant="staged")
    st = svc.wait(ex)
    assert st["execution"]["state"] == "succeeded", st["execution"]["error"]
    att = [a for a in st["attempts"] if a["node_id"] == "linked_binds"]
    assert len(att) == 1  # never blindly resubmitted
    assert att[0]["reconciliation"]["resolution"] == "adopted query id from tagged history"
    assert att[0]["query_id"] in att[0]["reconciliation"]["history_matches"]


def test_worker_crash_lease_expiry_reconciles_running_query(make_service):
    svc = make_service(lease_seconds=0.5)
    rid = _planned(svc)
    svc.runtime.faults["die_after_submit"] = {"cohort"}
    ex = svc.execute(rid, variant="staged")
    st = svc.wait(ex, timeout=60)
    assert st["execution"]["state"] == "succeeded", st["execution"]["error"]
    cohort_attempts = [a for a in st["attempts"] if a["node_id"] == "cohort"]
    assert len(cohort_attempts) == 1 and cohort_attempts[0]["state"] == "complete"
    stages = [e["stage"] for e in svc.trace.events(execution_id=ex)]
    assert "lease_expired:cohort" in stages and "reconcile:cohort" in stages


def test_failure_retries_after_known_failure(svc):
    rid = _planned(svc)
    svc.engine.faults["fail"] = [('"node":"outcome"', '"attempt":1,')]
    ex = svc.execute(rid, variant="staged")
    st = svc.wait(ex)
    assert st["execution"]["state"] == "succeeded"
    assert [a["state"] for a in st["attempts"] if a["node_id"] == "outcome"] == ["failed", "complete"]


def test_cancellation_stops_running_query(svc):
    rid = _planned(svc)
    svc.engine.faults["delay"] = {'"node":"cohort"': 1.5}
    ex = svc.execute(rid, variant="staged")
    time.sleep(0.3)
    svc.cancel(ex)
    st = svc.wait(ex, timeout=30)
    assert st["execution"]["state"] == "cancelled"
    assert not svc.cache.entries()


def test_expired_delegation_pauses_without_service_fallback(svc):
    rid = _planned(svc)
    svc.engine.expire_token("analyst_nc")
    ex = svc.execute(rid, variant="staged")
    deadline = time.time() + 10
    while time.time() < deadline and svc.runtime.status(ex)["execution"]["state"] != "paused_authorization":
        time.sleep(0.05)
    assert svc.runtime.status(ex)["execution"]["state"] == "paused_authorization"
    assert not [a for a in svc.runtime.status(ex)["attempts"] if a["query_id"]]
    svc.engine.renew_token("analyst_nc")
    svc.runtime.resume(ex)
    assert svc.wait(ex)["execution"]["state"] == "succeeded"


def test_shared_producer_survives_consumer_cancellation(svc):
    rid = _planned(svc)
    svc.engine.faults["delay"] = {'"node":"cohort"': 1.0}
    ex1 = svc.execute(rid, variant="staged")
    time.sleep(0.2)
    ex2 = svc.execute(rid, variant="staged")
    nodes2 = {n["node_id"]: n for n in svc.runtime.status(ex2)["nodes"]}
    assert nodes2["cohort"]["shared_from"] == f"{ex1}:cohort"
    out = svc.cancel(ex1)  # producer execution cancelled; the producer node has a waiter
    assert "cohort" in out["producers_kept_for_waiters"] or svc.runtime.status(ex1)["nodes"][0]["state"] in ("running", "reserved", "complete")
    st2 = svc.wait(ex2, timeout=30)
    assert st2["execution"]["state"] == "succeeded", st2["execution"]["error"]
    assert svc.wait(ex1, timeout=30)["execution"]["state"] == "cancelled"


def test_outbox_starts_workflow_after_restart(make_service):
    svc = make_service(start_runtime=False)
    rid = _planned(svc)
    ex = svc.execute(rid)
    assert svc.runtime.status(ex)["execution"]["state"] == "admitted"  # durable before any worker runs
    svc.runtime.start()
    assert svc.wait(ex)["execution"]["state"] == "succeeded"


def test_dispatch_and_admission_latency_recorded(svc):
    rid = _planned(svc)
    ex = svc.execute(rid)
    svc.wait(ex)
    r = svc.result(ex)["receipt"]
    assert r["latency"]["admission_ms"] < 1000
    assert r["latency"]["first_dispatch_ms"] is not None


def test_grain_violation_assertion_blocks(svc):
    svc.engine.admin_execute("CREATE TABLE stg_test_dup AS SELECT 'a' AS opportunity_id UNION ALL SELECT 'a'")
    sess = svc.engine.acquire("analyst_nc", "pricing_review")
    try:
        v = node_assertions(sess, {"assertions": ["a.grain_unique_cohort"], "params": {}}, "stg_test_dup")
    finally:
        svc.engine.release(sess)
    assert v["a.grain_unique_cohort"]["passed"] is False and v["a.grain_unique_cohort"]["value"] == 1
