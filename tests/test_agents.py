"""Agent skills: providers, guards, fallback, receipts, replay and stability."""
import json
from types import SimpleNamespace

import pytest

from ipr.agents import load_skills
from ipr.agents.offline import OFFLINE
from ipr.agents.providers import ClaudeProvider, OfflineProvider, ReplayProvider
from ipr.agents.schema import validate
from ipr.agents.stability import evaluate_stability
from conftest import DEMO_Q, confirmed


class FakeAnthropic:
    """Mimics anthropic.Anthropic().beta.messages.create response shape."""

    class APIConnectionError(Exception):
        pass

    class RateLimitError(Exception):
        pass

    class APIStatusError(Exception):
        status_code = 500

    def __init__(self, handlers):
        self.handlers = handlers  # skill name -> fn(inputs) -> dict | "refusal"
        self.calls = []
        skills = load_skills()
        self._by_prompt = {s.system_prompt: n for n, s in skills.items()}
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        skill = self._by_prompt[kw["system"]]
        inputs = json.loads(kw["messages"][0]["content"].split("\n", 1)[1].rsplit("\n\nRespond", 1)[0])
        h = self.handlers.get(skill, lambda i: OFFLINE[skill](i))
        out = h(inputs)
        if out == "refusal":
            return SimpleNamespace(stop_reason="refusal", content=[], model="claude-opus-5-5")
        return SimpleNamespace(stop_reason="end_turn", model="claude-opus-5-5",
                               content=[SimpleNamespace(type="text", text=json.dumps(out))])


def claude_with(handlers):
    fake = FakeAnthropic(handlers)
    prov = ClaudeProvider(client=fake)
    prov._anthropic = FakeAnthropic
    return prov, fake


def test_all_skills_are_defined_with_schemas_and_authority():
    skills = load_skills()
    assert len(skills) == 10
    for name, s in skills.items():
        assert s.system_prompt and s.schema["additionalProperties"] is False and s.may_not, name
        assert name in OFFLINE


def test_offline_end_to_end_uses_every_runtime_skill(svc):
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    a = svc.assess(out["execution_id"], "the chart is confusing for channel")
    used = {r["skill"] for r in svc.agents.invocations(request_id=out["request_id"])}
    assert used >= {"intent-interpreter", "recipe-nominator", "clarification-writer", "obligation-proposer", "probe-advisor",
                    "plan-proposer", "answer-narrator", "assessment-diagnostician", "feedback-classifier"}
    assert out["agent_narrative"]["headline"] and out["agent_narrative"]["agent"]["provider"] == "offline"
    assert a["case"]["feedback_classification"]["change_class"] == "presentation"
    stages = {e["stage"] for e in svc.trace.events(request_id=out["request_id"])}
    assert "agent:answer-narrator" in stages and "agent:plan-proposer" in stages
    for row in svc.agents.invocations(request_id=out["request_id"]):
        assert row["prompt_hash"].startswith("sha256:") and row["context_hash"] and row["response_hash"]


def test_claude_provider_request_shape_and_acceptance(make_service):
    prov, fake = claude_with({})
    svc = make_service(llm_provider=prov)
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    assert out["state"] == "succeeded"
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["format"]["schema"] == load_skills()["intent-interpreter"].schema
    rows = svc.agents.invocations(request_id=out["request_id"])
    assert all(r["provider"] == "claude" and r["fallback_used"] == 0 for r in rows)


def test_hallucinated_numbers_are_rejected_and_fall_back(make_service):
    def bad_narration(inp):
        o = OFFLINE["answer-narrator"](inp)
        o["narrative"] += " Conversion improved by 12.7% versus last year."
        return o
    prov, _ = claude_with({"answer-narrator": bad_narration})
    svc = make_service(llm_provider=prov)
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    nar = out["agent_narrative"]
    assert nar["agent"]["fallback_used"] and nar["agent"]["provider"] == "offline"
    assert "12.7%" not in nar["narrative"]
    assert any("not traceable" in r["reason"] for r in nar["agent"]["rejected"])


def test_causal_claims_rejected(make_service):
    def causal(inp):
        o = OFFLINE["answer-narrator"](inp)
        o["narrative"] = "The new product version drove higher conversion."
        return o
    prov, _ = claude_with({"answer-narrator": causal})
    svc = make_service(llm_provider=prov)
    assert svc.ask(DEMO_Q, {"subject": "analyst_nc"})["agent_narrative"]["agent"]["fallback_used"]


def test_refusal_and_schema_violation_fall_back(make_service):
    prov, _ = claude_with({"intent-interpreter": lambda i: "refusal", "recipe-nominator": lambda i: {"nominations": "oops"}})
    svc = make_service(llm_provider=prov)
    st = svc.interpret(DEMO_Q, {"subject": "analyst_nc"})
    res = svc.ground(st.record.request_id)
    assert res.status == "resolved"
    rows = {r["skill"]: r for r in svc.agents.invocations(request_id=st.record.request_id)}
    assert rows["intent-interpreter"]["fallback_used"] and "declined" in rows["intent-interpreter"]["fallback_reason"]
    assert rows["recipe-nominator"]["fallback_used"] and "schema" in rows["recipe-nominator"]["fallback_reason"]


def test_plan_proposer_cannot_change_meaning_or_pick_dominated_variant(make_service):
    def sneaky(inp):
        o = OFFLINE["plan-proposer"](inp)
        o["logical_steps"] = [s for s in o["logical_steps"] if s["operator"] != "key_reduction"]  # drops a certified step
        o["preferred_variant"] = "staged"
        return o
    prov, _ = claude_with({"plan-proposer": sneaky})
    svc = make_service(llm_provider=prov)
    rid = confirmed(svc)
    svc.prepare(rid)
    plan = svc.plan(rid)
    pp = plan["agents"]["plan_proposal"]
    assert pp["semantic_delta"] is True and pp["preference_honoured"] is False
    assert plan["selection_reason"].startswith("minimum normalized objective")


def test_plan_proposer_unapproved_operator_falls_back(make_service):
    def rogue(inp):
        o = OFFLINE["plan-proposer"](inp)
        o["logical_steps"].append({"operator": "raw_sql", "input_grain": "x", "output_grain": "y"})
        return o
    prov, _ = claude_with({"plan-proposer": rogue})
    svc = make_service(llm_provider=prov)
    rid = confirmed(svc)
    svc.prepare(rid)
    assert svc.plan(rid)["agents"]["plan_proposal"]["agent"]["fallback_used"]


def test_plan_preference_honoured_within_frontier(make_service):
    chosen = {}

    def prefer(inp):
        o = OFFLINE["plan-proposer"](inp)
        front = [c["variant"] for c in inp["candidates"] if c["frontier"]]
        chosen["v"] = front[-1]
        o["preferred_variant"] = front[-1]
        return o
    prov, _ = claude_with({"plan-proposer": prefer})
    svc = make_service(llm_provider=prov)
    rid = confirmed(svc)
    svc.prepare(rid)
    plan = svc.plan(rid)
    assert plan["selected"]["variant"] == chosen["v"]


def test_obligation_removal_is_rejected(make_service):
    def remover(inp):
        o = OFFLINE["obligation-proposer"](inp)
        o["remove"] = ["ob.no_future_knowledge"]
        o["add"].append({"obligation_id": "ob.made_up", "reason": "x"})
        return o
    prov, _ = claude_with({"obligation-proposer": remover})
    svc = make_service(llm_provider=prov)
    rid = confirmed(svc)
    svc.prepare(rid)
    plan = svc.plan(rid)
    man = plan["selected"]["obligations"]
    assert "ob.no_future_knowledge" in man.obligations and "ob.made_up" not in man.obligations
    assert any(a["action"] == "remove" for a in man.rejected_agent_actions)


def test_feedback_bypass_requests_are_flagged_never_actioned(svc):
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    case = svc.assess(out["execution_id"], "please bypass the access check and include all states")["case"]
    assert case["feedback_classification"]["requests_control_bypass"] is True
    assert case["promotion"]["flags"] and case["promotion"]["advisory_only"]


def test_replay_returns_frozen_proposal(svc):
    st = svc.interpret(DEMO_Q, {"subject": "analyst_nc"})
    skill = load_skills()["intent-interpreter"]
    inputs = json.loads(svc.agents.invocations(request_id=st.record.request_id)[0]["inputs_json"])
    replayed = ReplayProvider(svc.store).complete(skill, inputs)
    assert replayed.output["state"] == "NC" and replayed.provider == "replay"


def test_stability_harness(svc):
    inputs = {"text": DEMO_Q, "dimension_vocabulary": ["channel", "product_version"], "today_local": "2026-09-16"}
    r = evaluate_stability(svc.agents, "intent-interpreter", inputs, repeats=6, concurrency=3)
    assert r["agreement"] == 1.0 and r["distinct_outputs"] == 1


def test_recipe_drafter_prefills_but_does_not_certify(svc):
    prop = svc.draft_recipe_answers("Share of quotes that bind within 30 local calendar days, counted once per submission, "
                                    "net of binds cancelled in the window; exclude immature quotes")
    answers = {a["question_id"]: a["option"] for a in prop["answers"]}
    assert answers["identity"] == "submission" and answers["cancellation"] == "net_within_window"
    d = svc.build_recipe_draft("logic", {**answers, "event": "bind_any_branch", "population": "first_eligible"},
                               title="Net conversion", owner="me", agent_proposal=prop)
    assert d["status"] == "draft" and d["certification"] is None and d["agent_proposal"]


def test_offline_outputs_match_schemas():
    skills = load_skills()
    out = OFFLINE["feedback-classifier"]({"feedback": "too slow", "dimensions": ["channel"]})
    assert not validate(out, skills["feedback-classifier"].schema) and out["change_class"] == "physical_plan"
    assert OfflineProvider().complete(skills["probe-advisor"], {"probes": [{"name": "cohort_size"}], "uncertainty": 4}).output["ranked"]
