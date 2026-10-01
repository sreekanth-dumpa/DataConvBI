"""Control-plane service: the section 8 routing protocol, phase by phase.

Interpret -> Ground (metadata only, Pass A) -> Clarify/Confirm (semantic hash)
-> Retrieve & resolve (bindings, requester access, source cut, temporal
admission) -> Check reuse (information cache, saved plans) -> Plan (logical IR,
Pass B, admitted probes, certified physical variants, estimates, Pareto choice)
-> Execute (durable admission into the online runtime) -> Validate & cache ->
Present -> Assess -> Improve. Every terminal gap returns explicitly; semantic
edits invalidate confirmation and loop back to grounding.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import COMPILER_VERSION
from .access import AccessProvider
from .assessment import build_document, compute_metrics, gate_vector, new_case, recommendations
from .cache import InformationCache, rollup
from .canonical import fingerprint, short
from .compiler import CompileError, build_logical, build_physical, check_edges, replan_suffix
from .config import Settings
from .cost import BudgetBroker, Calibration, CostModel, WarehousePool, choose
from .cuts import bind_ports, select_cut, temporal_admission
from .engines.sqlite_engine import SQLiteEngine
from .feedback import FeedbackDirectory
from .fixture import build_fixture
from .grounding import Grounder, Resolution, end_of_local_day_utc
from .intent import (
    Clarification,
    Confirmation,
    Cohort,
    IntentBody,
    IntentRecord,
    Requester,
    Window,
    apply_edits,
    confirmation_matches,
    coverage_key,
    new_request_id,
    semantic_hash,
    semantic_payload,
)
from .llm import IntentProposer, RuleBasedProposer
from .monitoring import snapshot
from .obligations import ObligationCompiler
from .oracle import oracle
from .presentation import answer_facts, caveat_list, chart_spec, confidence_categories, narrative
from .profiling import Profiler, registered_estimates
from .recipe_builder import RecipeBuilder
from .registry import load_registry
from .rewrite import certify_physical, compare_results
from .runtime import OnlineRuntime
from .store import Store, dumps, loads
from .trace import TraceStore
from .validation import final_assertions, stage_registration

RUNTIME_CAPS = {"stage_registry", "publication_protocol", "presentation_facts"}


class Gap(Exception):
    def __init__(self, kind: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.detail = detail or {}


@dataclass
class RequestState:
    record: IntentRecord
    resolution: Resolution | None = None
    pending_cutoff_date: str | None = None
    prepared: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    executions: list[str] = field(default_factory=list)
    interpretation: dict[str, Any] | None = None


class IPRService:
    def __init__(self, settings: Settings | None = None, proposer: IntentProposer | None = None,
                 certify_access_classes: bool = False, start_runtime: bool = True):
        self.settings = settings or Settings()
        s = self.settings
        s.ensure_dirs()
        self.fixture_fingerprint = build_fixture(s.data_db, s.fixture_submissions, s.fixture_seed)
        self.clock = s.clock
        self.registry = load_registry()
        self.engine = SQLiteEngine(s.data_db, self.registry)
        self.store = Store(s.control_db)
        self.trace = TraceStore(self.store, self.clock)
        self.access = AccessProvider(self.engine, certify_classes=certify_access_classes)
        self.cache = InformationCache(self.store, self.access, self.clock, s.cache_ttl_days)
        self.grounder = Grounder(self.registry, self.clock)
        self.obligations = ObligationCompiler(self.registry)
        self.policy = self.registry.budget_policies["warehouse_admission_v2"]
        self.pool = WarehousePool(self.policy["auto_suspend_seconds"])
        self.broker = BudgetBroker(self.policy, self.pool)
        self.calibration = Calibration()
        self.cost = CostModel(self.policy, self.calibration)
        self.profiler = Profiler(self.store, self.broker, self.clock)
        self.feedback = FeedbackDirectory(s.feedback_dir)
        self.builder = RecipeBuilder(s.recipes_dir / "drafts")
        self.proposer = proposer or RuleBasedProposer()
        self.runtime = OnlineRuntime(self.store, self.engine, self.trace, self.clock, workers=s.workers,
                                     lease_seconds=s.lease_seconds, heartbeat_seconds=s.heartbeat_seconds,
                                     poll_seconds=s.poll_seconds, stage_ttl_hours=s.stage_ttl_hours,
                                     max_attempts=self.policy["max_attempts_per_node"])
        self.runtime.handlers = {"validate": self._h_validate, "publish": self._h_publish, "present": self._h_present}
        self.runtime.adaptation_policy = self._adaptation_policy
        self.runtime.on_stage_cost = self._stage_cost
        self.adaptation = {"enabled": True, "min_cohort_rows": 20000, "partitions": 4, "force": False, "margin": 0.2}
        self.requests: dict[str, RequestState] = {}
        self._exec_ctx: dict[str, dict[str, Any]] = {}
        if start_runtime:
            self.runtime.start()

    def close(self) -> None:
        self.runtime.stop()
        self.engine.close()

    # ================================================================ interpret
    def interpret(self, text: str, requester: Requester | dict, *, conversation_id: str | None = None,
                  parent_request_id: str | None = None, form: dict[str, Any] | None = None) -> RequestState:
        requester = requester if isinstance(requester, Requester) else Requester(**requester)
        body, assumptions, receipt = self.proposer.propose(text)
        if form:
            body.update({k: v for k, v in form.items() if v is not None})
        rid = new_request_id()
        ib = IntentBody(
            metric=body.get("metric"), scope=body.get("scope", {}),
            cohort=Cohort(**body["cohort"]) if body.get("cohort") else None,
            window=Window(value=int(body["window"]["value"])) if body.get("window") else None,
            knowledge_cutoff=body.get("knowledge_cutoff"), dimensions=list(dict.fromkeys(body.get("dimensions", []))),
            requested_recipe=body.get("requested_recipe"))
        if body.get("cut_request"):
            ib.cut_request.mode = body["cut_request"].get("mode", "pinned")
        rec = IntentRecord(request_id=rid, conversation_id=conversation_id or "conv_" + uuid.uuid4().hex[:8],
                           parent_request_id=parent_request_id, raw_request_ref=f"artifacts/requests/{rid}.txt", raw_text=text,
                           requester=requester, intent=ib, assumptions=assumptions)
        (self.settings.artifacts_dir / "requests").mkdir(parents=True, exist_ok=True)
        (self.settings.artifacts_dir / "requests" / f"{rid}.txt").write_text(text)
        st = RequestState(record=rec, pending_cutoff_date=body.get("knowledge_cutoff_local_date") if not body.get("knowledge_cutoff") else None,
                          interpretation=receipt.model_dump())
        self.requests[rid] = st
        self.store.x("INSERT INTO requests VALUES (?,?,?,?,?,?,?)", (rid, rec.conversation_id, parent_request_id, requester.subject, text,
                                                                    self.clock.now_z(), dumps(receipt.model_dump())))
        self._persist_intent(rec, "draft")
        self.trace.span(rid, "interpret", decision={"draft_fields": sorted(k for k, v in body.items() if v)},
                        output_refs={"interpretation_receipt": receipt.model_dump()}, status="proposed",
                        actor={"type": "agent" if receipt.interpreter != "rule_based" else "service", "version": receipt.interpreter})
        return st

    def _persist_intent(self, rec: IntentRecord, status: str) -> None:
        self.store.x("INSERT OR REPLACE INTO intents VALUES (?,?,?,?,?,?)",
                     (rec.request_id, rec.version, rec.model_dump_json(), semantic_hash(rec), status, self.clock.now_z()))

    # ================================================================ ground
    def ground(self, request_id: str) -> Resolution:
        st = self.requests[request_id]
        res = self.grounder.ground(st.record)
        if res.route and st.pending_cutoff_date and not st.record.intent.knowledge_cutoff:
            tz = res.bound_fields["business_timezone"]
            st.record.intent.knowledge_cutoff = end_of_local_day_utc(st.pending_cutoff_date, tz)
            st.record.assumptions.append(f"'as of {st.pending_cutoff_date}' interpreted as end of that local day in {tz}")
            st.pending_cutoff_date = None
            res = self.grounder.ground(st.record)
        if res.route:
            self._bind_definitions(st.record, res)
            res.advisory_feedback = self.feedback.retrieve(res.route["recipe_id"], st.record.intent.domains, st.record.intent.scope)
        # merge open questions into the record (material clarifications)
        known = {c.field for c in st.record.clarifications if c.status == "resolved"}
        st.record.clarifications = [c for c in st.record.clarifications if c.status == "resolved"] + \
            [q for q in res.questions if q.field not in known]
        st.resolution = res
        self.store.x("INSERT INTO resolutions VALUES (?,?,?,?)", (request_id, st.record.version, dumps(res.to_dict()), self.clock.now_z()))
        if res.obligations_a:
            self.store.x("INSERT OR REPLACE INTO obligation_manifests VALUES (?,?,?,?,?,?)",
                         (f"om_{request_id}_A_{st.record.version}", request_id, "A", dumps(res.obligations_a.to_dict()), None, self.clock.now_z()))
        self._persist_intent(st.record, res.status)
        self.trace.span(request_id, "ground", status=res.status,
                        decision={"route": res.route, "questions": [q.id for q in res.questions], "unsupported": res.unsupported,
                                  "gaps": res.gaps},
                        output_refs={"evidence_claims": res.evidence_manifest.get("selected_claims"),
                                     "obligations_a": sorted(res.obligations_a.obligations) if res.obligations_a else []},
                        input_refs={"registry_versions": self.registry.versions})
        return res

    def _bind_definitions(self, rec: IntentRecord, res: Resolution) -> None:
        b = res.bound_fields
        i = rec.intent
        i.metric = b["metric"]
        i.population = b["population"]
        i.quote_identity = b["quote_identity"]
        i.attribution_rule = b["attribution_rule"]
        i.bind_definition = b["bind_definition"]
        i.cancellation_treatment = b["cancellation_treatment"]
        i.business_timezone = b["business_timezone"]
        if b["window"]["value"] is not None:
            i.window = Window(**b["window"])
        i.time_basis = b["time_basis"]
        i.maturity_policy = b["maturity_policy"]
        i.domains = b["domains"]
        i.output_grain = list(i.dimensions)
        i.resolved_recipes = b["resolved_recipes"]
        if rec.confirmation.status == "confirmed" and rec.confirmation.approved_semantic_hash != semantic_hash(rec):
            rec.confirmation = Confirmation(status="invalidated")

    def clarify(self, request_id: str, answers: dict[str, Any]) -> Resolution:
        """Apply answers to material clarification questions (semantic edits)."""
        st = self.requests[request_id]
        edits = {}
        for fld, val in answers.items():
            if fld == "cohort" and isinstance(val, dict):
                edits["cohort"] = val
            elif fld == "window.value":
                edits["window"] = {**(st.record.intent.window.model_dump() if st.record.intent.window else Window().model_dump()), "value": int(val)}
            else:
                edits[fld] = val
            st.record.clarifications = [c for c in st.record.clarifications if c.field != fld] + [
                Clarification(id=f"a_{fld}", field=fld, question=f"answer for {fld}", answer=val, status="resolved")]
        rec, changed = apply_edits(st.record, edits)
        st.record = rec
        st.prepared = st.plan = None
        self.trace.span(request_id, "clarify", decision={"answers": answers, "semantic_changed": changed})
        return self.ground(request_id)

    def edit(self, request_id: str, edits: dict[str, Any]) -> Resolution:
        st = self.requests[request_id]
        rec, changed = apply_edits(st.record, edits)
        st.record = rec
        if changed:
            st.prepared = st.plan = None
        self.trace.span(request_id, "semantic_edit", decision={"edits": edits, "invalidated_confirmation": changed})
        return self.ground(request_id)

    def confirm(self, request_id: str, actor: str) -> str:
        st = self.requests[request_id]
        res = st.resolution
        if res is None or res.status != "resolved":
            raise Gap("confirmation_blocked", "Missing or unsupported meaning blocks confirmation",
                      {"status": res.status if res else None, "questions": [q.model_dump() for q in (res.questions if res else [])],
                       "gaps": res.gaps if res else []})
        h = semantic_hash(st.record)
        st.record.confirmation = Confirmation(status="confirmed", approved_semantic_hash=h, actor=actor, at=self.clock.now_z())
        self.store.x("INSERT INTO confirmations VALUES (?,?,?,?,?)", (request_id, h, actor, self.clock.now_z(), "confirmed"))
        self._persist_intent(st.record, "confirmed")
        self.trace.span(request_id, "confirm", decision={"semantic_hash": h, "actor": actor})
        return h

    # ================================================================ resolve + reuse
    def prepare(self, request_id: str) -> dict[str, Any]:
        st = self.requests[request_id]
        rec = st.record
        if not confirmation_matches(rec):
            raise Gap("not_confirmed", "Confirmed meaning does not match the current semantic hash; reconfirm")
        recipe = self.registry.recipe(rec.intent.resolved_recipes[0]["id"], rec.intent.resolved_recipes[0]["version"])
        bindings = bind_ports(self.registry, recipe)
        ports = [b["port_ref"] for b in bindings]
        access = self.access.establish(rec.requester, ports)
        if not access.allowed:
            raise Gap("access_denied", "; ".join(access.reasons), {"access": access.to_dict()})
        now = self.clock.now_z()
        cut = select_cut(self.engine, self.registry, bindings, rec.intent.knowledge_cutoff, rec.intent.cut_request.mode, now)
        ok, reasons = temporal_admission(cut, rec.intent.knowledge_cutoff, now)
        if not ok:
            self.trace.span(request_id, "temporal_admission", status="rejected", decision={"reasons": reasons})
            raise Gap("temporal_admission", "Pinned execution cannot be admitted: " + "; ".join(reasons),
                      {"offer": "Edit cut_request.mode to 'latest' for a provisional answer (not cached as pinned)", "reasons": reasons})
        decision = self.cache.lookup(rec, recipe, cut, access, rec.requester, ports)
        template_key = self._plan_template_key(rec, recipe)
        saved = self.store.one("SELECT l.plan_id, l.version, a.applicability_json FROM logical_plans l JOIN plan_applicability a "
                               "ON a.plan_id=l.plan_id WHERE l.plan_id=? AND a.certified=1 ORDER BY l.version DESC LIMIT 1",
                               (template_key,))
        saved_ok = None
        if saved:
            app = loads(saved["applicability_json"])
            saved_ok = app.get("registry_versions") == self.registry.versions and app.get("recipe") == self.registry.recipe_ref(recipe)
        st.prepared = {"recipe": recipe, "bindings": bindings, "access": access, "cut": cut, "cache": decision,
                       "saved_plan": {"plan_id": template_key, "found": bool(saved), "applicable": saved_ok}}
        self.trace.span(request_id, "resolve_and_reuse", cut_mode=cut.cut_mode, replayable_until=cut.replayable_until,
                        decision={"cache": decision.to_dict(), "saved_plan": st.prepared["saved_plan"]},
                        output_refs={"source_cut": cut.fingerprint, "access": access.fingerprint})
        return st.prepared

    def reuse_answer(self, request_id: str) -> dict[str, Any]:
        """Validate and present a cache hit with a cache-operation receipt."""
        st = self.requests[request_id]
        p = st.prepared
        d = p["cache"]
        rec, recipe = st.record, p["recipe"]
        dims = list(rec.intent.dimensions)
        rows = d.rows
        # validate the reuse IR's obligations: conservation and num<=den on the (derived) rows
        violations = [r for r in rows if r["converted"] > r["eligible_mature"] or
                      r["eligible_all"] != r["eligible_mature"] + r["immature"] + r["quality_excluded"]]
        if violations:
            raise Gap("reuse_validation_failed", "cached information set failed reuse validation")
        cut = p["cut"]
        facts = answer_facts(rows, dims, origin=f"cache_{d.outcome}", information_set_id=d.entry["information_set_id"],
                             cut=cut.to_dict(), intent=rec.intent.model_dump(mode="json"), recipe=recipe)
        op_id = "cacheop_" + uuid.uuid4().hex[:10]
        receipt = {
            "receipt_kind": "cache_operation", "cache_operation_id": op_id, "request_id": request_id,
            "confirmed_semantic_hash": rec.confirmation.approved_semantic_hash, "information_set_id": d.entry["information_set_id"],
            "reuse": d.outcome, "rule": d.rule, "producer_receipt_ref": d.entry["manifest"]["producer_receipt_ref"],
            "producer_execution_id": d.entry["manifest"]["producer_execution_id"], "reauthorization": d.reauthorization,
            "availability": d.availability, "source_cut": cut.to_dict(), "access_fingerprint": p["access"].fingerprint,
            "rejected_candidates": d.rejected, "answer_facts": facts, "result_digest": fingerprint(rows),
        }
        self.store.x("INSERT OR REPLACE INTO receipts VALUES (?,?,?)", (op_id, dumps(receipt), self.clock.now_z()))
        self.trace.span(request_id, "present_from_cache", decision={"information_set_id": d.entry["information_set_id"], "rule": d.rule})
        return {"origin": f"cache_{d.outcome}", "facts": facts, "chart": chart_spec(facts), "narrative": narrative(facts, recipe),
                "confidence": confidence_categories(confirmed=True, recipe_status=recipe["status"], cut_mode=cut.cut_mode,
                                                    temporal_ok=True, structural_ok=True),
                "receipt": receipt}

    # ================================================================ plan
    @staticmethod
    def _plan_template_key(rec: IntentRecord, recipe: dict[str, Any]) -> str:
        return "plan_" + short(fingerprint({"recipe": recipe["id"], "version": recipe["version"], "dims": rec.intent.dimensions,
                                            "algebra": recipe["algebra"]}), 16)

    def plan(self, request_id: str, *, variants: tuple[str, ...] = ("fused", "staged", "partitioned"), n_partitions: int = 4,
             force_probes: bool = False, agent_proposal: dict[str, list[str]] | None = None) -> dict[str, Any]:
        st = self.requests[request_id]
        rec = st.record
        if not confirmation_matches(rec):
            raise Gap("not_confirmed", "Confirmed meaning changed; reconfirm before planning")
        if st.prepared is None:
            self.prepare(request_id)
        p = st.prepared
        recipe, cut, access = p["recipe"], p["cut"], p["access"]
        logical = build_logical(rec, recipe, self.registry)
        # proposed meaning must equal the confirmed meaning
        if logical["params"]["window_days"] != rec.intent.window.value or logical["dimensions"] != rec.intent.dimensions:
            raise Gap("semantic_delta", "Planner meaning differs from confirmed meaning; reconfirmation required")
        edge_problems = check_edges(logical)
        if edge_problems:
            raise Gap("uncertified_plan", "Logical composition is not certified: " + "; ".join(edge_problems))
        man_a = st.resolution.obligations_a
        if agent_proposal:
            man_a.merge_agent_proposal(self.registry, **agent_proposal)
        # admitted probes under the requester's identity (after confirmation)
        sess = self.engine.acquire(rec.requester.subject, rec.requester.purpose)
        try:
            stats = self.profiler.collect(sess, logical, cut, access, rec.constraints.discovery_target_credits,
                                          self.policy["max_probes"], registered_estimates(self.registry, rec), force_probes)
        finally:
            self.engine.release(sess)
        if stats.get("canonical_duplicates"):
            raise Gap("identity_violation", "Canonical selection produced duplicate opportunity keys on this cut")
        candidates = []
        base_ctx = {"evidence": st.resolution.evidence_manifest.get("selected_claims", []), "route_resolved": True,
                    "products_ok": True, "access_decision": access.allowed, "cut_admitted": cut.admitted,
                    "runtime": RUNTIME_CAPS, "compiler_checks": {"edge_check"}, "policies": set(self.registry.policies)}
        warm = self.pool.is_warm(self.policy["default_class"])
        for v in variants:
            phys = build_physical(logical, v, stats, self.engine.dialect, n_partitions)
            cert = certify_physical(phys, logical, self.registry)
            man_b = self.obligations.pass_b(man_a, logical, phys)
            man_b.evaluate({**base_ctx, "certificate": set(cert["certified_obligations"])})
            blocking = man_b.blocking()
            est = self.cost.estimate(phys, stats, self.settings.workers, warm)
            candidates.append({"variant": v, "physical": phys, "certificate": cert, "obligations": man_b, "estimate": est,
                               "admissible": cert["admitted"] and not blocking,
                               "blocking": [{"id": b.id, "reason": b.reason} for b in blocking]})
        admissible = [c for c in candidates if c["admissible"]]
        if not admissible:
            raise Gap("no_admissible_path", "No admissible execution path",
                      {"blocking": {c["variant"]: c["blocking"] for c in candidates}})
        chosen, front = choose([c["estimate"] for c in admissible], rec.constraints.execution_target_credits,
                               rec.constraints.max_runtime_seconds)
        if chosen is None:
            raise Gap("budget", "No candidate fits the execution allocation or runtime limit",
                      {"estimates": [c["estimate"].to_dict() for c in admissible]})
        selected = next(c for c in admissible if c["estimate"] is chosen)
        selected["obligations"].freeze()
        plan_id = self._plan_template_key(rec, recipe)
        version = (self.store.one("SELECT MAX(version) AS v FROM logical_plans WHERE plan_id=?", (plan_id,))["v"] or 0) + 1
        now = self.clock.now_z()
        physical_ids = {}
        with self.store.tx() as con:
            con.execute("INSERT INTO logical_plans VALUES (?,?,?,?,?,?,?,?,?)",
                        (plan_id, version, semantic_hash(rec), coverage_key(rec), logical["logical_fingerprint"], recipe["id"],
                         recipe["version"], dumps(logical), now))
            for c in candidates:
                pid = f"phys_{short(c['physical']['physical_fingerprint'], 14)}_{uuid.uuid4().hex[:4]}"
                physical_ids[c["variant"]] = pid
                con.execute("INSERT INTO physical_plans VALUES (?,?,?,?,?,?,?,?,?)",
                            (pid, plan_id, version, c["variant"], c["physical"]["physical_fingerprint"], dumps(c["physical"]),
                             dumps(c["certificate"]), None, now))
            con.execute("INSERT OR REPLACE INTO plan_applicability VALUES (?,?,?,?)", (plan_id, dumps({
                "parameter_schema": sorted(logical["params"]), "recipe": self.registry.recipe_ref(recipe),
                "product_scope": recipe["applicability"], "registry_versions": self.registry.versions,
                "policy_dependencies": access.policy_dependencies, "statistics_envelope": {k: stats.get(k) for k in
                                                                                         ("cohort_rows", "linked_bind_rows", "bind_multiplicity_max")},
                "certification": "certified_composition"}), 1, now))
            con.execute("INSERT OR REPLACE INTO obligation_manifests VALUES (?,?,?,?,?,?)",
                        (f"om_{request_id}_B_{plan_id}_{version}", request_id, "B", dumps(selected["obligations"].to_dict()),
                         selected["obligations"].frozen_hash, now))
        st.plan = {"plan_id": plan_id, "version": version, "logical": logical, "candidates": candidates, "selected": selected,
                   "physical_ids": physical_ids, "stats": stats, "frontier": [e.variant for e in front],
                   "semantic_hash": semantic_hash(rec), "saved_plan_reused": p["saved_plan"]["found"] and p["saved_plan"]["applicable"]}
        self.trace.span(request_id, "physical_planning", status="proposed",
                        input_refs={"intent_hash": semantic_hash(rec), "evidence_manifest": st.resolution.evidence_manifest.get("selected_claims"),
                                    "stats_source": stats.get("source")},
                        decision={"selected_plan": selected["variant"], "frontier": st.plan["frontier"],
                                  "rejected": [{"plan": c["variant"], "rule": c["blocking"] or c["estimate"].infeasible_reasons or "dominated/objective"}
                                               for c in candidates if c is not selected]},
                        output_refs={"logical_ir_hash": logical["logical_fingerprint"], "physical_dag_hash": selected["physical"]["physical_fingerprint"],
                                     "obligations_b": selected["obligations"].frozen_hash},
                        expectations={"duration_seconds_p50": selected["estimate"].latency_p50_s, "duration_seconds_p90": selected["estimate"].latency_p90_s,
                                      "credits_range": [selected["estimate"].credits_low, selected["estimate"].credits_high]},
                        cut_mode=cut.cut_mode, replayable_until=cut.replayable_until)
        return st.plan

    # ================================================================ execute
    def execute(self, request_id: str, *, variant: str | None = None, publish: bool = True, share: bool = True) -> str:
        st = self.requests[request_id]
        rec = st.record
        if not confirmation_matches(rec) or st.plan is None or st.plan["semantic_hash"] != semantic_hash(rec):
            raise Gap("not_admitted", "Plan or confirmation is stale for the current semantic hash")
        cand = st.plan["selected"] if variant is None else next(c for c in st.plan["candidates"] if c["variant"] == variant)
        if not cand["admissible"]:
            raise Gap("not_admitted", f"variant {cand['variant']} is not admissible")
        p = st.prepared
        ex_id = "exec_" + uuid.uuid4().hex[:10]
        est = cand["estimate"]
        if not self.broker.reserve(ex_id, est.resume_floor_credits + self.policy["service_overhead_credits"], rec.constraints.execution_target_credits):
            raise Gap("budget", "admission reservation refused")
        self._exec_ctx[ex_id] = {"request_id": request_id, "recipe": p["recipe"], "cut": p["cut"], "access": p["access"],
                                 "record": rec.model_copy(deep=True), "candidate": cand, "plan": st.plan, "publish": publish,
                                 "t_admit": time.perf_counter(), "timeline": {}}
        cut_d = p["cut"].to_dict()
        acc_d = {**p["access"].to_dict(), "reuse_key": p["access"].reuse_key}
        adm = self.runtime.admit(execution_id=ex_id, request_id=request_id, plan_id=st.plan["plan_id"],
                                 physical_id=st.plan["physical_ids"][cand["variant"]], physical=cand["physical"],
                                 principal=rec.requester.subject, purpose=rec.requester.purpose, tenant=rec.requester.tenant,
                                 cut=cut_d, access=acc_d,
                                 budget={"execution_target_credits": rec.constraints.execution_target_credits, "publish": publish,
                                         "policy": rec.budget_policy_id, "reserved": self.broker.reserved(ex_id)},
                                 estimate=est.to_dict(), plan_version=1, share=share)
        self._exec_ctx[ex_id]["admission"] = adm
        st.executions.append(ex_id)
        self.trace.span(request_id, "admission", execution_id=ex_id, cut_mode=p["cut"].cut_mode, replayable_until=p["cut"].replayable_until,
                        decision={"variant": cand["variant"], "workflow_id": adm["workflow_id"], "publish": publish},
                        observations={"admission_latency_ms": adm["admission_latency_ms"]})
        return ex_id

    def _stage_cost(self, ex_id: str, spec: dict[str, Any]) -> bool:
        ctx = self._ctx(ex_id)
        secs = self.cost.node_seconds(spec, ctx["plan"]["stats"])
        rate = self.policy["warehouse_rates_credits_per_hour"].get(spec.get("resource_class") or "XSMALL", 1) / 3600
        self.pool.touch(spec.get("resource_class") or "XSMALL")
        return self.broker.reserve(ex_id, secs * rate, ctx["record"].constraints.execution_target_credits)

    def _ctx(self, ex_id: str) -> dict[str, Any]:
        if ex_id not in self._exec_ctx:
            raise Gap("context_lost", f"execution context for {ex_id} not in this process (restart recovery not supported in the POC)")
        return self._exec_ctx[ex_id]

    # ---------------------------------------------------------------- runtime handlers
    def _h_validate(self, execution: dict, spec: dict, context: dict) -> dict:
        ctx = self._ctx(execution["execution_id"])
        rec = ctx["record"]
        rels = context["relations"]
        final_rel = rels[spec["final_node"]]
        final_node = next(n for n in loads(self.store.one("SELECT plan_json FROM physical_plans WHERE physical_id=?",
                                                          (execution["physical_id"],))["plan_json"])["nodes"] if n["id"] == spec["final_node"])
        sess = self.engine.acquire(execution["principal"], execution["purpose"])
        try:
            verdicts = final_assertions(sess, final_rel, rec.intent.knowledge_cutoff, rels, final_node["assertions"])
        finally:
            self.engine.release(sess)
        stage_rels = [r["relation"] for r in self.store.q("SELECT relation FROM stages WHERE execution_id=? AND state='complete'",
                                                          (execution["execution_id"],))]
        verdicts["a.stage_registered"] = stage_registration(self.store, execution["execution_id"], execution["principal"], stage_rels)
        for row in self.store.q("SELECT observations_json FROM nodes WHERE execution_id=? AND state='complete'", (execution["execution_id"],)):
            for k, v in ((loads(row["observations_json"]) or {}).get("assertions") or {}).items():
                verdicts.setdefault(k, v)
        ok = all(v["passed"] for v in verdicts.values())
        ctx["validation"] = verdicts
        ctx["timeline"]["validated"] = time.perf_counter()
        self.trace.span(ctx["request_id"], "validate", execution_id=execution["execution_id"], status="ok" if ok else "failed",
                        decision={k: v["passed"] for k, v in verdicts.items()})
        return {"ok": ok, "error": None if ok else f"validation failed: {[k for k, v in verdicts.items() if not v['passed']]}",
                "verdicts": verdicts}

    def _h_publish(self, execution: dict, spec: dict, context: dict) -> dict:
        ctx = self._ctx(execution["execution_id"])
        rec, recipe = ctx["record"], ctx["recipe"]
        ex_id = execution["execution_id"]
        final_rel = context["relations"][spec["final_node"]]
        rows = self.engine.read_relation(final_rel, limit=rec.intent.output.row_limit)
        ctx["rows"] = rows
        output_key = f"{ex_id}:result"
        digest = fingerprint([{k: r[k] for k in r if k not in ("max_knowledge_at", "max_bind_knowledge_at")} for r in rows])
        try:
            self.store.x("INSERT INTO result_manifests VALUES (?,?,?,?,?)", (ex_id, output_key, dumps({"relation": final_rel, "rows": len(rows)}),
                                                                           digest, self.clock.now_z()))
        except Exception:  # unique output key: compare-and-set publication
            return {"ok": False, "error": "result manifest already published for this execution"}
        ctx["result_digest"] = digest
        if not ctx["publish"]:
            return {"ok": True, "published": False, "reason": "replay/comparison execution; cache publication disabled"}
        if ctx["cut"].cut_mode != "pinned":
            ctx["information_set"] = None
            return {"ok": True, "published": False, "reason": "provisional cut: not published as a pinned information set"}
        res_rel = f"res_{ex_id.replace('exec_', '')}"
        self.engine.rename_relation(final_rel, res_rel)
        self.store.x("UPDATE stages SET relation=?, state='published', expires_at=? WHERE relation=?",
                     (res_rel, None, final_rel))
        self.store.x("UPDATE nodes SET output_relation=? WHERE execution_id=? AND output_relation=?", (res_rel, ex_id, final_rel))
        manifest = self.cache.publish(execution_id=ex_id, rec=rec, recipe=recipe, cut=ctx["cut"], access=ctx["access"], rows=rows,
                                      validation_passed=all(v["passed"] for v in ctx.get("validation", {}).values()), complete=True,
                                      relation_ref=res_rel, receipt_ref=f"receipts/{ex_id}.json")
        ctx["information_set"] = manifest
        return {"ok": True, "published": bool(manifest), "information_set_id": manifest["information_set_id"] if manifest else None}

    def _h_present(self, execution: dict, spec: dict, context: dict) -> dict:
        ex_id = execution["execution_id"]
        ctx = self._ctx(ex_id)
        rec, recipe = ctx["record"], ctx["recipe"]
        dims = list(rec.intent.dimensions)
        rows = rollup(ctx["rows"], dims)  # normalizes counts and recomputes rates (no-op grouping)
        facts = answer_facts(rows, dims, origin="fresh_execution",
                             information_set_id=(ctx.get("information_set") or {}).get("information_set_id"),
                             cut=ctx["cut"].to_dict(), intent=rec.intent.model_dump(mode="json"), recipe=recipe)
        v = ctx.get("validation", {})
        temporal_ok = all(v.get(k, {"passed": True})["passed"] for k in ("a.no_future_knowledge", "a.window_respected"))
        structural_ok = all(x["passed"] for k, x in v.items() if k not in ("a.no_future_knowledge", "a.window_respected"))
        ctx["presentation"] = {"origin": "fresh_execution", "facts": facts, "chart": chart_spec(facts), "narrative": narrative(facts, recipe),
                               "confidence": confidence_categories(confirmed=True, recipe_status=recipe["status"], cut_mode=ctx["cut"].cut_mode,
                                                                   temporal_ok=temporal_ok, structural_ok=structural_ok)}
        ctx["timeline"]["presented"] = time.perf_counter()
        receipt = self._build_receipt(ex_id)
        ctx["presentation"]["receipt"] = receipt
        self.store.x("INSERT OR REPLACE INTO receipts VALUES (?,?,?)", (ex_id, dumps(receipt), self.clock.now_z()))
        for n in receipt["nodes"]:
            if n.get("kind") == "sql_ctas" and n.get("observations", {}).get("elapsed_ms") is not None and n.get("estimate_s"):
                self.calibration.observe(n["operator"], n["estimate_s"], n["observations"]["elapsed_ms"] / 1000)
        self.trace.span(ctx["request_id"], "present", execution_id=ex_id, decision={"origin": "fresh_execution"},
                        output_refs={"result_digest": receipt["result_digest"], "information_set_id": facts["information_set_id"]})
        return {"ok": True, "facts_digest": fingerprint(facts)}

    # ---------------------------------------------------------------- receipts
    def _build_receipt(self, ex_id: str) -> dict[str, Any]:
        ctx = self._ctx(ex_id)
        rec, recipe, cut, access = ctx["record"], ctx["recipe"], ctx["cut"], ctx["access"]
        status = self.runtime.status(ex_id)
        ex = status["execution"]
        phys = loads(self.store.one("SELECT plan_json FROM physical_plans WHERE physical_id=?", (ex["physical_id"],))["plan_json"])
        stats = ctx["plan"]["stats"]
        node_specs = {n["id"]: n for n in phys["nodes"]}
        nodes = []
        for n in status["nodes"]:
            spec = node_specs.get(n["node_id"]) or {}
            nodes.append({"node_id": n["node_id"], "state": n["state"], "kind": spec.get("kind"), "operator": spec.get("operator"),
                          "envelope": spec.get("envelope"), "estimate_s": round(self.cost.node_seconds(spec, stats), 5) if spec else None,
                          "observations": n["observations"], "attempts": n["attempts"], "shared_from": n["shared_from"],
                          "plan_version": n["plan_version"]})
        attempts = [{"attempt_id": a["attempt_id"], "node_id": a["node_id"], "state": a["state"], "query_id": a["query_id"],
                     "query_tag": a["query_tag"], "output_relation": a["output_relation"], "reconciliation": a["reconciliation"]}
                    for a in status["attempts"]]
        adaptations = [loads(r["adaptation_json"]) for r in self.store.q("SELECT adaptation_json FROM adaptations WHERE execution_id=? ORDER BY version", (ex_id,))]
        t = ctx["timeline"]
        total = (t.get("presented", time.perf_counter()) - ctx["t_admit"])
        first_dispatch = min((a["metrics"] or {}).get("dispatch_latency_ms") or 0 for a in status["attempts"]) if status["attempts"] else None
        manifest_b = ctx["candidate"]["obligations"].copy("B")
        manifest_b.evaluate({"evidence": self.requests[ctx["request_id"]].resolution.evidence_manifest.get("selected_claims", []),
                             "route_resolved": True, "products_ok": True, "access_decision": access.allowed, "cut_admitted": cut.admitted,
                             "certificate": set(ctx["candidate"]["certificate"]["certified_obligations"]), "runtime": RUNTIME_CAPS,
                             "compiler_checks": {"edge_check"}, "policies": set(self.registry.policies),
                             "validated": {k: v["passed"] for k, v in ctx.get("validation", {}).items()}})
        sim_credits = round(self.broker.reserved(ex_id), 6)
        facts = ctx["presentation"]["facts"]
        return {
            "receipt_kind": "execution", "execution_id": ex_id, "request_id": ctx["request_id"], "workflow_id": ex["workflow_id"],
            "confirmed_semantic_hash": rec.confirmation.approved_semantic_hash,
            "route": {"recipe_id": recipe["id"], "version": recipe["version"], "binding_version": recipe["binding_version"],
                      "certificate_ref": recipe["certification"]["certificate_ref"]},
            "versions": {"registries": self.registry.versions, "compiler": COMPILER_VERSION, "fixture": self.fixture_fingerprint,
                         "product_contracts": {f"{e.product_id}/{e.port_id}": e.contract_version for e in cut.entries}},
            "source_cut": cut.to_dict(), "cut_mode": cut.cut_mode, "replayable_until": cut.replayable_until,
            "retention_checked_at": cut.entries[0].retention_checked_at if cut.entries else None,
            "access": {"subject": access.subject, "fingerprint": access.fingerprint, "reuse_mode": access.reuse_mode,
                       "policy_version": access.policy_version, "entitlement_epoch": access.entitlement_epoch,
                       "policy_dependencies": access.policy_dependencies, "allowed": access.allowed},
            "proposal": ctx["candidate"]["estimate"].to_dict() | {"variant": ctx["candidate"]["variant"]},
            "initial_plan": {"variant": ctx["candidate"]["variant"], "physical_fingerprint": ctx["candidate"]["physical"]["physical_fingerprint"]},
            "actual_plan": {"variant": phys["variant"], "physical_fingerprint": phys["physical_fingerprint"], "plan_version": ex["plan_version"]},
            "certificate": ctx["candidate"]["certificate"], "adaptations": adaptations, "nodes": nodes, "attempts": attempts,
            "obligations": manifest_b.to_dict(), "validation": ctx.get("validation", {}),
            "statistics": {"source": stats.get("source"), "cohort_rows": stats.get("cohort_rows"),
                           "bind_multiplicity": {k: stats.get(k) for k in ("bind_multiplicity_p50", "bind_multiplicity_p95", "bind_multiplicity_max")},
                           "probe_log": stats.get("probe_log")},
            "estimate": ctx["candidate"]["estimate"].to_dict(),
            "latency": {"total_s": round(total, 4), "admission_ms": ctx["admission"]["admission_latency_ms"],
                        "first_dispatch_ms": first_dispatch, "validate_to_present_s": round(t.get("presented", 0) - t.get("validated", 0), 4) if t.get("validated") else None},
            "cost": {"reserved_simulated_credits": sim_credits, "provisional_simulated_credits": sim_credits,
                     "attribution": "pending — local engine has no billing source; values are simulated credit-equivalents"},
            "origin": "fresh_execution", "reused_artifacts": [n["shared_from"] for n in nodes if n["shared_from"]],
            "output_grain": rec.intent.dimensions, "exclusions_by_dimension": [
                {**{d: r[d] for d in rec.intent.dimensions}, "immature": r["immature"], "quality_excluded": r["quality_excluded"]}
                for r in facts["by_dimension"]],
            "completeness": "complete", "exactness": "exact_for_confirmed_mature_population",
            "result_digest": ctx.get("result_digest"), "information_set": ctx.get("information_set"),
            "artifact_expiry": (ctx.get("information_set") or {}).get("expires_at"),
            "answer_facts": facts, "caveats": caveat_list(facts, recipe),
            "verification_scope": {"rewrite_rules": [r["rule_id"] for r in ctx["candidate"]["certificate"]["rules"]],
                                   "bounded_verifier": ctx["candidate"]["certificate"]["verification"]["verdict"],
                                   "business_truth": "unverified"},
            "telemetry": {"spill": "unavailable: not observable in local engine", "operator_stats": "collected for completed statements"},
            "note": "A receipt documents evidence; it is not a mathematical proof of business truth.",
        }

    # ================================================================ adaptation policy
    def _adaptation_policy(self, ex: dict, physical: dict, info: dict) -> dict | None:
        cfg = self.adaptation
        if not cfg["enabled"] or not {"cohort", "linked_binds"} <= info["completed"]:
            return None
        if "adapted" in physical["variant"] or physical["variant"] == "fused":
            return None
        ctx = self._exec_ctx.get(ex["execution_id"])
        if ctx is None or ex["revisions"] >= ctx["record"].constraints.max_plan_revisions:
            return None
        obs = info["observations"]
        cohort_rows = (obs.get("cohort") or {}).get("rows") or 0
        bind_rows = (obs.get("linked_binds") or {}).get("rows") or 0
        stats = {**ctx["plan"]["stats"], "cohort_rows": cohort_rows, "linked_bind_rows": bind_rows, "uncertainty": 1.2}
        current_suffix = [n for n in physical["nodes"] if n["id"] not in info["completed"]]
        if physical["variant"] == "staged":
            target_n = cfg["partitions"]
            trigger = cohort_rows >= cfg["min_cohort_rows"] or cfg["force"]
        else:  # partitioned: combine tiny branches
            target_n = 1 if cohort_rows < cfg["min_cohort_rows"] // 10 else physical["n_partitions"]
            trigger = (target_n != physical["n_partitions"]) or cfg["force"]
            if cfg["force"]:
                target_n = cfg.get("partitions", 2)
        if not trigger or target_n == physical.get("n_partitions"):
            return None
        new_plan = replan_suffix(physical, info["completed"], target_n)
        cur_cost = sum(self.cost.node_seconds(n, stats) for n in current_suffix if n["kind"] == "sql_ctas")
        new_suffix = [n for n in new_plan["nodes"] if n["id"] not in info["completed"] and n["kind"] == "sql_ctas"]
        par = max((self.cost.node_seconds(n, stats) for n in new_suffix if n.get("partition") is not None), default=0)
        new_cost = par * max(1, target_n / self.settings.workers) + sum(self.cost.node_seconds(n, stats) for n in new_suffix if n.get("partition") is None)
        overhead = 0.02 * target_n
        saving_lb = (cur_cost - new_cost) / 1.5 - overhead
        if not cfg["force"] and saving_lb <= cfg["margin"] * cur_cost:
            return None
        logical = ctx["plan"]["logical"]
        cert = certify_physical(new_plan, logical, self.registry)
        assert new_plan["logical_fingerprint"] == logical["logical_fingerprint"], "adaptation must preserve confirmed semantics"
        if not cert["admitted"]:
            return None
        record = {"trigger": "forced" if cfg["force"] else "observed volume outside validity range",
                  "checkpoint": {"cohort_rows": cohort_rows, "linked_bind_rows": bind_rows,
                                 "validity_range": {"staged_preferred_below_cohort_rows": cfg["min_cohort_rows"]}},
                  "changed_suffix": [n["id"] for n in current_suffix], "new_suffix": [n["id"] for n in new_suffix],
                  "expected_payoff": {"current_suffix_s": round(cur_cost, 4), "new_suffix_s": round(new_cost, 4),
                                      "lower_bound_saving_s": round(saving_lb, 4)},
                  "equivalence": {"rules": [r["rule_id"] for r in cert["rules"]], "same_logical_fingerprint": True,
                                  "source_cut_and_access_unchanged": True},
                  "from_variant": physical["variant"], "to_variant": new_plan["variant"], "partitions": target_n}
        return {"plan": new_plan, "certificate": cert, "record": record,
                "physical_id": f"phys_{short(new_plan['physical_fingerprint'], 14)}_{uuid.uuid4().hex[:4]}"}

    # ================================================================ results / status
    def wait(self, ex_id: str, timeout: float = 120) -> dict[str, Any]:
        return self.runtime.wait(ex_id, timeout)

    def result(self, ex_id: str) -> dict[str, Any]:
        ctx = self._ctx(ex_id)
        status = self.runtime.status(ex_id)
        if status["execution"]["state"] != "succeeded":
            return {"state": status["execution"]["state"], "error": status["execution"]["error"], "status": status}
        return {"state": "succeeded", **ctx["presentation"]}

    def cancel(self, ex_id: str) -> dict[str, Any]:
        return self.runtime.cancel(ex_id)

    # ================================================================ convenience: whole protocol
    def ask(self, text: str, requester: Requester | dict, *, answers: dict[str, Any] | None = None, auto_confirm: bool = True,
            variant: str | None = None, wait: bool = True, form: dict | None = None, conversation_id: str | None = None,
            parent_request_id: str | None = None, timeout: float = 120) -> dict[str, Any]:
        st = self.interpret(text, requester, conversation_id=conversation_id, parent_request_id=parent_request_id, form=form)
        rid = st.record.request_id
        res = self.ground(rid)
        if res.status == "clarification_required" and answers:
            res = self.clarify(rid, {k: v for k, v in answers.items() if any(q.field == k for q in res.questions)} or answers)
        if res.status != "resolved":
            return {"request_id": rid, "stage": "ground", "status": res.status, "questions": [q.model_dump() for q in res.questions],
                    "gaps": res.gaps, "unsupported": res.unsupported}
        if not auto_confirm:
            return {"request_id": rid, "stage": "confirm", "status": "awaiting_confirmation", "intent": st.record.intent.model_dump(mode="json")}
        self.confirm(rid, actor=st.record.requester.subject)
        try:
            prep = self.prepare(rid)
        except Gap as g:
            return {"request_id": rid, "stage": "prepare", "status": g.kind, "message": str(g), "detail": g.detail}
        if prep["cache"].outcome != "miss":
            return {"request_id": rid, "stage": "reuse", "status": "succeeded", **self.reuse_answer(rid)}
        try:
            self.plan(rid)
        except Gap as g:
            return {"request_id": rid, "stage": "plan", "status": g.kind, "message": str(g), "detail": g.detail}
        ex_id = self.execute(rid, variant=variant)
        if not wait:
            return {"request_id": rid, "stage": "execute", "status": "running", "execution_id": ex_id}
        self.wait(ex_id, timeout)
        return {"request_id": rid, "stage": "execute", "execution_id": ex_id, **self.result(ex_id)}

    # ================================================================ assessment & replay
    def replay(self, ex_id: str, variant: str) -> str:
        """Execute another admitted variant on the identical source/access cut (no cache publication)."""
        ctx = self._ctx(ex_id)
        return self.execute(ctx["request_id"], variant=variant, publish=False, share=False)

    def run_oracle(self, ex_id: str) -> dict[str, Any]:
        ctx = self._ctx(ex_id)
        rec, recipe, cut, access = ctx["record"], ctx["recipe"], ctx["cut"], ctx["access"]
        params = {"state": rec.intent.scope["state"], "lob": rec.intent.scope["lob"], "cohort_start": rec.intent.cohort.start,
                  "cohort_end_exclusive": rec.intent.cohort.end_exclusive, "window_value": rec.intent.window.value,
                  "knowledge_cutoff": rec.intent.knowledge_cutoff, "dimensions": rec.intent.dimensions}
        seqs = cut.seq_by_port
        return oracle(str(self.settings.data_db), params, recipe["algebra"]["identity"], recipe["algebra"]["timezone"],
                      seqs["quote_fdp/quoted_opportunities_v2"], seqs["policy_fdp/known_bind_events_v2"], set(access.effective_states))

    def assess(self, ex_id: str, user_feedback: str | None = None, *, compare_variants: bool = False, run_oracle: bool = True) -> dict[str, Any]:
        ctx = self._ctx(ex_id)
        receipt = loads(self.store.one("SELECT receipt_json FROM receipts WHERE execution_id=?", (ex_id,))["receipt_json"])
        rows = ctx["rows"]
        dims = list(ctx["record"].intent.dimensions)
        context: dict[str, Any] = {"trace_valid": self.trace.verify()["valid"]}
        if run_oracle:
            exp = self.run_oracle(ex_id)
            cmp = compare_results(rows, exp["rows"], dims)
            context["oracle_agreement"] = {"value": cmp["equivalent"], "reason": None, "differences": cmp["differences"][:5],
                                           "oracle": "independent plain-Python implementation (ipr/oracle.py)"}
        if compare_variants:
            agree, measured = [], {}
            for v in ("fused", "staged", "partitioned"):
                rid = self.replay(ex_id, v)
                self.wait(rid)
                rctx = self._ctx(rid)
                if "rows" not in rctx:
                    agree.append({"variant": v, "equivalent": None, "reason": "replay failed"})
                    continue
                cmpv = compare_results(rows, rctx["rows"], dims)
                agree.append({"variant": v, "equivalent": cmpv["equivalent"]})
                r = loads(self.store.one("SELECT receipt_json FROM receipts WHERE execution_id=?", (rid,))["receipt_json"])
                measured[v] = r["latency"]["total_s"]
            context["plan_equivalence"] = {"value": all(a["equivalent"] for a in agree), "reason": None, "variants": agree,
                                           "scope": "same source cut and access; exact count statistics"}
            sel = receipt["actual_plan"]["variant"].split("+")[0]
            if sel in measured:
                context["regret"] = {"value": round(measured[sel] - min(measured.values()), 4), "reason": None,
                                     "measured_latency_s": measured, "note": "single replay each; not a statistically powered comparison"}
        same = self.store.q("SELECT e.execution_id FROM executions e WHERE e.state='succeeded' AND e.execution_id<>?", (ex_id,))
        comparable = []
        for e in same:
            r = self.store.one("SELECT receipt_json FROM receipts WHERE execution_id=?", (e["execution_id"],))
            if not r:
                continue
            rr = loads(r["receipt_json"])
            if rr.get("confirmed_semantic_hash") == receipt["confirmed_semantic_hash"] and rr["source_cut"]["fingerprint"] == receipt["source_cut"]["fingerprint"] \
                    and rr["access"]["fingerprint"] == receipt["access"]["fingerprint"]:
                comparable.append(rr["result_digest"] == receipt["result_digest"])
        if comparable:
            context["consistency"] = {"value": round(sum(comparable) / len(comparable), 4), "reason": None,
                                      "numerator": sum(comparable), "denominator": len(comparable)}
        metrics = compute_metrics(receipt, context)
        gates = gate_vector(receipt, context)
        recs = recommendations(receipt, metrics, gates)
        doc = build_document(receipt, metrics, gates, recs, user_feedback)
        case = new_case(receipt, metrics, gates, recs, user_feedback, ctx["record"].intent.model_dump(mode="json"))
        if context.get("oracle_agreement") and not context["oracle_agreement"]["value"]:
            case["counterexample_inputs"] = {"source_cut": receipt["source_cut"]["fingerprint"], "params": ctx["plan"]["logical"]["params"]}
            case["expected_outputs"] = self.run_oracle(ex_id)["rows"]
        folder = self.settings.feedback_dir / case["case_id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "assessment.md").write_text(doc)
        self.feedback.save(case)
        self.store.x("INSERT INTO assessments VALUES (?,?,?,?,?,?)", (case["case_id"], ex_id, ctx["request_id"], str(folder / "assessment.md"),
                                                                    dumps(case), self.clock.now_z()))
        self.trace.span(ctx["request_id"], "assess", execution_id=ex_id, decision={"case_id": case["case_id"], "gates": gates})
        return {"case": case, "document": doc, "path": str(folder)}

    # ================================================================ misc
    def monitoring(self) -> dict[str, Any]:
        return snapshot(self.store, self.clock)

    def set_policy_mapping(self, subject: str, states: list[str], purpose: str = "pricing_review", bump_epoch: bool = True) -> None:
        """Administrative change to the (query-time) row access mapping."""
        self.engine.admin_execute("DELETE FROM policy_state_mapping WHERE subject=? AND purpose=?", (subject, purpose))
        for s in states:
            self.engine.admin_execute("INSERT INTO policy_state_mapping VALUES (?,?,?)", (subject, s, purpose))
        if bump_epoch:
            cur = int(self.engine.admin_query("SELECT value FROM policy_meta WHERE key='entitlement_epoch'")[0]["value"])
            self.engine.admin_execute("UPDATE policy_meta SET value=? WHERE key='entitlement_epoch'", (str(cur + 1),))

    def bump_policy_version(self, version: str) -> int:
        self.engine.admin_execute("UPDATE policy_meta SET value=? WHERE key='policy_version'", (version,))
        return self.cache.invalidate_policy_change(version)

    def request_summary(self, request_id: str) -> dict[str, Any]:
        st = self.requests[request_id]
        return {"request_id": request_id, "intent": st.record.model_dump(mode="json"), "semantic_hash": semantic_hash(st.record),
                "semantic_payload": semantic_payload(st.record),
                "resolution": st.resolution.to_dict() if st.resolution else None, "executions": st.executions}
