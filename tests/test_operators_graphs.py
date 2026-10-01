from ipr.pyoperators import PY_OPERATORS, wilson_interval
from conftest import DEMO_Q, confirmed


def test_wilson_interval_values():
    r = wilson_interval([{"eligible_mature": 100, "converted": 30}], {"confidence_level": 0.95})[0]
    assert abs(r["ci_low"] - 0.2189) < 1e-3 and abs(r["ci_high"] - 0.3958) < 1e-3
    assert wilson_interval([{"eligible_mature": 0, "converted": 0}], {})[0]["ci_low"] is None


def test_python_operator_runs_pinned_and_validated(svc):
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    r = out["receipt"]
    assert r["validation"]["a.python_operator_digest"]["passed"]
    py = next(n for n in r["nodes"] if n["node_id"] == "py_wilson_interval")
    assert py["observations"]["code_digest"] == PY_OPERATORS["wilson_interval"]["code_digest"]
    assert py["observations"]["determinism"] == "deterministic_float_tolerance"
    assert all(x["ci_low"] <= x["conversion_rate"] <= x["ci_high"] for x in out["facts"]["by_dimension"] if x["eligible_mature"])
    assert "CI" in out["narrative"]


def test_intervals_recomputed_on_derived_cache_reuse(svc):
    svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    rolled = svc.ask("30-day quote conversion for NC personal auto quotes in August 2026 by channel, as of 2026-09-15",
                     {"subject": "analyst_nc"})
    assert rolled["origin"] == "cache_derived"
    assert all("ci_low" in x for x in rolled["facts"]["by_dimension"])


def test_tampered_python_operator_digest_fails_execution(svc):
    rid = confirmed(svc)
    svc.prepare(rid)
    plan = svc.plan(rid)
    for c in plan["candidates"]:
        for n in c["physical"]["nodes"]:
            if n["kind"] == "python_op":
                n["code_digest"] = "sha256:tampered"
    ex = svc.execute(rid)
    st = svc.wait(ex)
    assert st["execution"]["state"] == "failed" and "digest" in st["execution"]["error"]


def test_child_graph_decomposition_recorded(svc):
    rid = confirmed(svc)
    svc.prepare(rid)
    plan = svc.plan(rid)
    staged = next(c for c in plan["candidates"] if c["variant"] == "staged")["physical"]
    cg = staged["child_graphs"]
    assert cg["quote_cohort_dag"]["nodes"] == ["cohort"] and cg["linkage_dag"]["nodes"] == ["linked_binds"]
    assert "quote_cohort_dag->linkage_dag" in cg["_edges"] and "metric_dag->information_path" in cg["_edges"]
    fused = next(c for c in plan["candidates"] if c["variant"] == "fused")["physical"]
    assert set(fused["child_graphs"]) == {"fused_metric_dag", "information_path", "_edges"}
