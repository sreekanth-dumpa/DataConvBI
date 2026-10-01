import pytest

from ipr.compiler import CompileError, validate_sql
from ipr.recipe_builder import QUESTIONS, RecipeBuilder
from ipr.rewrite import tlp_check
from conftest import DEMO_Q


def test_sql_validator_rejects_bypass_and_dml():
    for bad in ("SELECT * FROM quote_branches", "DELETE FROM x", "SELECT 1; SELECT 2",
                "SELECT load_extension('x') FROM {{port:quote_fdp/quoted_opportunities_v2}} q"):
        with pytest.raises(CompileError):
            validate_sql(bad)


def test_tlp_metamorphic_check(svc):
    sess = svc.engine.acquire("analyst_nc", "pricing_review")
    try:
        r = tlp_check(sess, "{{port:policy_fdp/known_bind_events_v2}}", "t.cancelled_at > '2026-08-20T00:00:00Z'", {},
                      {"policy_fdp/known_bind_events_v2": 2}, projection="t.bind_event_id, t.cancelled_at")
    finally:
        svc.engine.release(sess)
    assert r["equal"] and r["rows"] > 0


def test_row_access_policy_applies_in_engine(svc):
    sess = svc.engine.acquire("analyst_sc", "pricing_review")
    try:
        rows = sess.fetch_all("SELECT DISTINCT state FROM {{port:quote_fdp/quoted_opportunities_v2}} q", {},
                              cut={"quote_fdp/quoted_opportunities_v2": 2})
    finally:
        svc.engine.release(sess)
    assert {r["state"] for r in rows} == {"SC"}


def test_trace_chain_detects_tampering(svc):
    svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    anchor = svc.trace.anchor()
    assert svc.trace.verify(anchor)["valid"]
    svc.store.x("UPDATE trace_events SET event_json = replace(event_json, 'ok', 'OK') WHERE seq = 3")
    assert not svc.trace.verify(anchor)["valid"]
    assert svc.trace.export_otel(svc.store.one("SELECT request_id FROM trace_events LIMIT 1")["request_id"])


def test_recipe_builder_drafts_are_never_certified(tmp_path):
    b = RecipeBuilder(tmp_path)
    assert len(b.questions()) == 6 == len(QUESTIONS)
    v2 = {"identity": "submission", "event": "bind_any_branch", "population": "first_eligible", "window": "local_half_open",
          "cancellation": "gross", "maturity": "exclude"}
    d = b.build_draft("same as certified", v2, title="Gross conversion", owner="me")
    assert d["status"] == "draft" and d["certification"] is None
    assert d["installed_algebra_match"]["recipe"] == {"id": "gross_submission_quote_conversion", "version": "2.0.0"}
    net = dict(v2, cancellation="net_within_window")
    d2 = b.build_draft("net", net, title="Net conversion", owner="me")
    assert not d2["installed_algebra_match"]["matches"]
    assert any("cancellation=net_within_window" in u for u in d2["installed_algebra_match"]["unsupported_meanings"])
    assert all(e["expected_outcome"] is None for e in d2["adversarial_examples"])


def test_feedback_lifecycle_and_scoped_retrieval(svc):
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    a = svc.assess(out["execution_id"], "please highlight immature exclusions")
    cid = a["case"]["case_id"]
    assert (svc.settings.feedback_dir / cid / "assessment.md").exists()
    assert svc.feedback.retrieve("gross_submission_quote_conversion", ["quote"], {"state": "NC"}) == []
    svc.feedback.transition(cid, "approved", "reviewer", "advisory")
    hits = svc.feedback.retrieve("gross_submission_quote_conversion", ["quote"], {"state": "NC", "lob": "personal_auto"})
    assert hits and hits[0]["advisory_only"]
    assert svc.feedback.retrieve("gross_submission_quote_conversion", ["quote"], {"state": "VA"}) == []
    st = svc.interpret(DEMO_Q, {"subject": "analyst_nc"})
    assert svc.ground(st.record.request_id).advisory_feedback


def test_semantic_change_cannot_activate_without_regression(svc):
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    case = svc.assess(out["execution_id"])["case"]
    case["promotion"]["change_class"] = "semantic_specification"
    svc.feedback.save(case)
    svc.feedback.transition(case["case_id"], "approved", "r")
    with pytest.raises(ValueError):
        svc.feedback.transition(case["case_id"], "active", "r", executable=True)


def test_monitoring_snapshot(svc):
    svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    svc.ask("NC personal auto loss ratio for August 2026", {"subject": "analyst_nc"})
    snap = svc.monitoring()
    m = snap["metrics"]
    assert m["requests"] == 2 and m["unsupported_route_rate"]["numerator"] == 1
    assert m["missing_receipts"] == 0
    assert any(t["metric"] == "spill_local_bytes" and t["state"] == "unavailable" for t in m["telemetry_availability"])
