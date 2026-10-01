import pytest

from ipr.compiler import build_logical
from ipr.obligations import ObligationCompiler
from ipr.registry import load_registry
from ipr.service import Gap
from conftest import DEMO_Q, confirmed


def test_missing_parameters_produce_material_questions(svc):
    st = svc.interpret("quote conversion by channel", {"subject": "analyst_nc"})
    res = svc.ground(st.record.request_id)
    assert res.status == "clarification_required"
    assert {q.field for q in res.questions} >= {"scope.state", "scope.lob", "cohort", "window.value", "knowledge_cutoff"}
    with pytest.raises(Gap):
        svc.confirm(st.record.request_id, "analyst_nc")
    res = svc.clarify(st.record.request_id, {"scope.state": "NC", "scope.lob": "personal_auto",
                                             "cohort": {"start": "2026-08-01", "end_exclusive": "2026-09-01"},
                                             "window.value": 30, "knowledge_cutoff": "2026-09-16T03:59:59Z"})
    assert res.status == "resolved"


def test_unsupported_dimension_is_visible_and_not_executed(svc):
    out = svc.ask("30-day quote conversion for NC personal auto quotes in August 2026 by driver age, as of 2026-09-15",
                  {"subject": "analyst_nc"})
    assert out["stage"] == "ground" and out["status"] == "unsupported"
    assert "dimension=driver_age" in out["unsupported"]
    assert not svc.store.q("SELECT * FROM executions")


def test_unknown_metric_yields_definition_gap_and_unresolved_obligations(svc):
    st = svc.interpret("What is the NC personal auto loss ratio for August 2026?", {"subject": "analyst_nc"})
    res = svc.ground(st.record.request_id)
    assert res.status in ("unsupported", "missing_definition")
    assert any("No certified recipe" in g for g in res.gaps)
    assert {"ob.metric_classification", "ob.definition_resolution"} <= set(res.obligations_a.obligations)


def test_recipe_grounding_binds_definitions_before_confirmation(svc):
    st = svc.interpret(DEMO_Q, {"subject": "analyst_nc"})
    res = svc.ground(st.record.request_id)
    i = svc.requests[st.record.request_id].record.intent
    assert res.status == "resolved"
    assert i.quote_identity["multi_version_policy"] == "count_once_across_branches"
    assert i.window.interval == "[start,end)" and i.business_timezone == "America/New_York"
    assert i.knowledge_cutoff == "2026-09-16T03:59:59Z"  # end of Sep 15 local
    assert set(res.evidence_manifest["selected_claims"]) == {"clm.quote_identity", "clm.bind_linkage", "clm.bind_definition",
                                                             "clm.temporal_window", "clm.analytical_algebra"}
    assert not res.evidence_manifest["missing_obligations"]


def test_edit_after_confirmation_requires_reconfirmation(svc):
    rid = confirmed(svc)
    svc.edit(rid, {"window.value": 45})
    with pytest.raises(Gap) as e:
        svc.prepare(rid)
    assert e.value.kind == "not_confirmed"


def test_agent_cannot_remove_or_waive_obligations(svc):
    rid = confirmed(svc)
    svc.prepare(rid)
    plan = svc.plan(rid, agent_proposal={"add": ["ob.tie_break_certified"], "remove": ["ob.no_future_knowledge"],
                                         "waive": ["ob.access"]})
    man = plan["selected"]["obligations"]
    assert "ob.no_future_knowledge" in man.obligations and "ob.access" in man.obligations
    assert {a["action"] for a in man.rejected_agent_actions} == {"remove", "waive"}


def test_pass_b_blocks_missing_operator_declaration(svc):
    reg = load_registry()
    rid = confirmed(svc)
    rec = svc.requests[rid].record
    logical = build_logical(rec, reg.recipe("gross_submission_quote_conversion"), reg)
    logical["nodes"][3]["operator"] = "fancy_unregistered_join"
    comp = ObligationCompiler(reg)
    man_b = comp.pass_b(svc.requests[rid].resolution.obligations_a, logical)
    man_b.evaluate({"evidence": ["clm.quote_identity"], "route_resolved": True})
    assert any(o.id.startswith("ob.operator_declaration_missing") for o in man_b.blocking())


def test_pass_a_closure_includes_implied_obligations():
    reg = load_registry()
    m = ObligationCompiler(reg).pass_a(["event_conversion_rate"], [reg.recipe("gross_submission_quote_conversion")], ["quote"])
    assert {"ob.cut_admission", "ob.identity_unique", "ob.exclusion_ledger_conservation", "ob.additive_algebra"} <= set(m.obligations)


def test_semantic_graph_explains_unsupported_dimension(svc):
    st = svc.interpret("30-day quote conversion for NC personal auto quotes in August 2026 by driver age, as of 2026-09-15",
                       {"subject": "analyst_nc"})
    res = svc.ground(st.record.request_id)
    assert res.status == "unsupported"
    assert any("no certified source mapping for driver_age" in g for g in res.gaps)


def test_semantic_graph_paths_for_supported_dimensions(svc):
    st = svc.interpret(DEMO_Q, {"subject": "analyst_nc"})
    res = svc.ground(st.record.request_id)
    paths = {p["dimension"]: p for p in res.semantic_paths}
    assert paths["channel"]["status"] == "supported" and paths["channel"]["attribution"] == "first_eligible_quoted_branch"
    assert paths["product_version"]["path"] == ["quote_branch->submission_opportunity (many_to_one)"]
