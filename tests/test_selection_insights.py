from ipr.selector import VariantSelector
from conftest import DEMO_Q, confirmed


def test_selector_is_rule_based_until_enough_receipts(svc):
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    learned = svc.result(out["execution_id"])["receipt"]["selection"]["learned"]
    assert learned["mode"] == "rule_based" and learned["selection_probability"] == 1.0


def test_selector_exploits_measured_best_with_recorded_probability(svc):
    sel = VariantSelector(svc.store, min_samples=2, epsilon=0.0)
    frontier = [{"variant": "fused", "latency_p90_s": 1, "credits_high": 0.01}, {"variant": "staged", "latency_p90_s": 2, "credits_high": 0.01}]
    for i, (v, t) in enumerate([("fused", 0.9), ("fused", 1.1), ("staged", 0.3), ("staged", 0.4)]):
        sel.observe("wl_x", v, t, 0.01, f"e{i}", "t")
    d = sel.select("wl_x", {"f": 1}, frontier, baseline="fused", seed="r1")
    assert d["mode"] == "exploit" and d["chosen"] == "staged" and d["baseline"] == "fused"
    assert d["arms"]["staged"]["n"] == 2


def test_query_insights_and_explain_in_receipt(svc):
    rid = confirmed(svc)
    svc.prepare(rid)
    plan = svc.plan(rid)
    staged = next(c for c in plan["candidates"] if c["variant"] == "staged")
    assert "cohort" in staged["explain"] and "linked_binds" not in staged["explain"]
    ex = svc.execute(rid, variant="staged")
    svc.wait(ex)
    r = svc.result(ex)["receipt"]
    ins = {i["node_id"]: {x["type"]: x for x in i["insights"]} for i in r["query_insights"]}
    assert ins["linked_binds"]["exploding_join"]["available"] is True
    assert ins["cohort"]["spill"]["available"] is False and ins["cohort"]["spill"]["reason"]
    assert ins["outcome"]["full_scan_predicate"]["available"] is False
