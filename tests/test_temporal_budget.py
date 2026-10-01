import pytest

from ipr.cost import BudgetBroker, WarehousePool
from ipr.registry import load_registry
from ipr.service import Gap
from conftest import confirmed

Q = "30-day quote conversion for NC personal auto quotes in August 2026 by channel"


def test_future_cutoff_is_not_admitted(svc):
    rid = confirmed(svc, Q + ", as of 2026-09-17")
    with pytest.raises(Gap) as e:
        svc.prepare(rid)
    assert e.value.kind == "temporal_admission"


def test_cutoff_after_watermark_but_latest_mode_is_provisional_and_uncached(svc):
    rid = confirmed(svc, Q + ", as of 2026-09-15", **{"cut_request": {"mode": "latest", "selection": "latest_available_publication"}})
    p = svc.prepare(rid)
    assert p["cut"].cut_mode == "latest_provisional"
    svc.plan(rid)
    ex = svc.execute(rid)
    svc.wait(ex)
    assert svc.result(ex)["facts"]["source_cut"]["cut_mode"] == "latest_provisional"
    assert not svc.cache.entries()


def test_incomplete_publication_is_skipped(svc):
    svc.engine.admin_execute("UPDATE product_publications SET complete=0 WHERE seq=2")
    rid = confirmed(svc, Q + ", as of 2026-09-09")
    p = svc.prepare(rid)
    assert {e.seq for e in p["cut"].entries} == {1}


def test_replay_horizon_recorded(svc):
    rid = confirmed(svc, Q + ", as of 2026-09-15")
    p = svc.prepare(rid)
    assert p["cut"].replayable_until == "2026-09-17T04:05:00Z"


def test_resume_floor_arithmetic_and_probe_downgrade():
    policy = load_registry().budget_policies["warehouse_admission_v2"]
    broker = BudgetBroker(policy, WarehousePool())
    assert round(broker.resume_floor("MEDIUM"), 4) == 0.0667
    d = broker.admit_probe(0.05, 0.5, "MEDIUM")
    assert not d["admitted"] and "XSMALL" in d["alternatives"]
    assert broker.admit_probe(0.05, 0.5, "XSMALL")["admitted"]


def test_discovery_allocation_too_small_uses_registered_statistics(svc):
    rid = confirmed(svc, Q + ", as of 2026-09-15")
    svc.requests[rid].record.constraints.discovery_target_credits = 0.001
    svc.prepare(rid)
    plan = svc.plan(rid)
    assert plan["stats"]["source"] == "registered_catalog_estimate"
    assert all(e["decision"] == "not_admitted" for e in plan["stats"]["probe_log"])


def test_execution_budget_exhaustion_fails_honestly(svc):
    rid = confirmed(svc, Q + ", as of 2026-09-15")
    svc.prepare(rid)
    plan = svc.plan(rid)
    staged = next(c for c in plan["candidates"] if c["variant"] == "staged")
    # allocation covers admission (resume floor + overhead) but no stage work
    svc.requests[rid].record.constraints.execution_target_credits = staged["estimate"].resume_floor_credits + 0.002
    ex = svc.execute(rid, variant="staged")
    st = svc.wait(ex)
    assert st["execution"]["state"] == "failed" and "budget" in st["execution"]["error"]
