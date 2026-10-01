"""Every admitted physical route agrees with the independent oracle on a fixed cut."""
import pytest

from ipr.rewrite import compare_results
from conftest import DEMO_Q, confirmed


@pytest.mark.parametrize("variant", ["fused", "staged", "partitioned"])
def test_variant_matches_oracle(svc, variant):
    rid = confirmed(svc)
    svc.prepare(rid)
    plan = svc.plan(rid)
    assert all(c["admissible"] for c in plan["candidates"])
    ex = svc.execute(rid, variant=variant)
    st = svc.wait(ex)
    assert st["execution"]["state"] == "succeeded", st["execution"]["error"]
    rows = svc._ctx(ex)["rows"]
    exp = svc.run_oracle(ex)["rows"]
    assert compare_results(rows, exp, ["product_version", "channel"])["equivalent"]
    receipt = svc.result(ex)["receipt"]
    assert all(v["passed"] for v in receipt["validation"].values())
    assert receipt["obligations"]["obligations"]["ob.no_future_knowledge"]["status"] == "validated"


def test_legacy_recipe_is_distinct_and_matches_oracle(svc):
    rid = confirmed(svc, "legacy quote conversion for NC personal auto quotes in August 2026 by channel, 30 day, as of 2026-09-15")
    assert svc.requests[rid].record.intent.resolved_recipes[0]["id"] == "quote_conversion"
    out_hash = svc.request_summary(rid)["semantic_hash"]
    svc.prepare(rid)
    svc.plan(rid)
    ex = svc.execute(rid)
    svc.wait(ex)
    assert compare_results(svc._ctx(ex)["rows"], svc.run_oracle(ex)["rows"], ["channel"])["equivalent"]
    rid2 = confirmed(svc, "quote conversion for NC personal auto quotes in August 2026 by channel, 30 day, as of 2026-09-15")
    assert svc.request_summary(rid2)["semantic_hash"] != out_hash  # different meanings never share a contract


def test_suffix_adaptation_preserves_result(svc):
    svc.adaptation.update(force=True, partitions=3)
    rid = confirmed(svc)
    svc.prepare(rid)
    svc.plan(rid)
    ex = svc.execute(rid, variant="staged")
    st = svc.wait(ex)
    assert st["execution"]["state"] == "succeeded", st["execution"]["error"]
    receipt = svc.result(ex)["receipt"]
    assert receipt["adaptations"] and receipt["adaptations"][0]["applied"]
    assert receipt["actual_plan"]["variant"] == "staged+adapted_partitioned"
    assert {n["node_id"] for n in receipt["nodes"] if n["state"] == "superseded"} == {"outcome", "metric_stats"}
    assert compare_results(svc._ctx(ex)["rows"], svc.run_oracle(ex)["rows"], ["product_version", "channel"])["equivalent"]


def test_partitioned_combines_tiny_branches(svc):
    svc.adaptation.update(min_cohort_rows=100000)
    rid = confirmed(svc)
    svc.prepare(rid)
    svc.plan(rid)
    ex = svc.execute(rid, variant="partitioned")
    svc.wait(ex)
    receipt = svc.result(ex)["receipt"]
    assert receipt["adaptations"] and receipt["adaptations"][0]["partitions"] == 1
    assert compare_results(svc._ctx(ex)["rows"], svc.run_oracle(ex)["rows"], ["product_version", "channel"])["equivalent"]


def test_ask_end_to_end_and_assessment(svc):
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    assert out["state"] == "succeeded" and out["origin"] == "fresh_execution"
    a = svc.assess(out["execution_id"], "looks plausible", compare_variants=True)
    assert a["case"]["gates"] == {"semantic_admission": "pass", "access": "pass", "temporal_consistency": "pass",
                                  "structural_validity": "pass", "result_verification": "pass"}
    assert a["case"]["metrics"]["plan_equivalence"]["value"] is True
    assert a["case"]["metrics"]["business_result_accuracy"]["value"] is None  # never fabricated
