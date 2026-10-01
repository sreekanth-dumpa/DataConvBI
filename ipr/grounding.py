"""Metadata-only grounding (sections 2, 5): route identification, hard
applicability, semantic search validated by the same rules, clarification
questions, recipe-definition binding and obligation-directed evidence
selection. No business source rows are read here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .canonical import sha256_hex, to_utc_z
from .intent import Clarification, IntentRecord, KNOWN_DIMENSIONS
from .obligations import ObligationCompiler, ObligationManifest
from .registry import Registry

TOKEN_RE = re.compile(r"[a-z0-9]+")
SEMANTIC_THRESHOLD = 0.12


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class Resolution:
    status: str  # resolved | clarification_required | unsupported | missing_definition
    route: dict[str, Any] | None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    applicability: list[dict[str, Any]] = field(default_factory=list)
    questions: list[Clarification] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    evidence_manifest: dict[str, Any] = field(default_factory=dict)
    obligations_a: ObligationManifest | None = None
    bound_fields: dict[str, Any] = field(default_factory=dict)
    advisory_feedback: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "route": self.route, "candidates": self.candidates, "applicability": self.applicability,
            "questions": [q.model_dump() for q in self.questions], "unsupported": self.unsupported, "missing": self.missing,
            "gaps": self.gaps, "evidence_manifest": self.evidence_manifest, "bound_fields": self.bound_fields,
            "obligations_a": self.obligations_a.to_dict() if self.obligations_a else None,
            "advisory_feedback": self.advisory_feedback,
        }


def end_of_local_day_utc(d: str, tz: str) -> str:
    local_next = datetime.combine(date.fromisoformat(d) + timedelta(days=1), datetime.min.time()).replace(tzinfo=ZoneInfo(tz))
    return to_utc_z(local_next - timedelta(seconds=1))


class Grounder:
    def __init__(self, registry: Registry, clock=None, metadata_visibility: tuple[str, ...] = ("public", "internal")):
        self.registry = registry
        self.compiler = ObligationCompiler(registry)
        self.clock = clock
        self.visibility = metadata_visibility

    # ---- route identification --------------------------------------------
    def candidate_recipes(self, rec: IntentRecord) -> list[dict[str, Any]]:
        text = rec.raw_text.lower()
        found: dict[str, dict[str, Any]] = {}
        requested = rec.intent.requested_recipe
        for r in self.registry.recipes:
            key = f"{r['id']}@{r['version']}"
            how = None
            if requested and r["id"] == requested:
                how = "explicit_request"
            elif any(a in text for a in r["intent_aliases"]):
                how = "verified_alias"
            elif any(re.search(p, rec.raw_text) for p in r["patterns"]):
                how = "parameterized_pattern"
            if how:
                found[key] = {"recipe": r, "matched_by": how, "similarity": None}
        # semantic search for additional candidates; never authoritative by itself
        for r in self.registry.recipes:
            key = f"{r['id']}@{r['version']}"
            if key in found:
                continue
            sim = _similarity(text, " ".join([r["title"], r["description"], " ".join(r["intent_aliases"])]))
            if sim >= SEMANTIC_THRESHOLD:
                found[key] = {"recipe": r, "matched_by": "semantic_search", "similarity": round(sim, 3)}
        conversion_metrics = {"quote_conversion"} | {r["id"] for r in self.registry.recipes if r["metric_type"] == "event_conversion_rate"}
        if rec.intent.metric and rec.intent.metric not in conversion_metrics and not requested:
            # a recognized non-conversion metric does not match conversion recipes
            found = {k: v for k, v in found.items() if v["matched_by"] == "explicit_request"}
        return list(found.values())

    def applicability(self, recipe: dict[str, Any], rec: IntentRecord, explicitly_requested: bool) -> dict[str, Any]:
        reasons, unsupported = [], []
        app = recipe["applicability"]
        scope = rec.intent.scope
        if recipe["status"] not in ("certified", "deprecated_supported"):
            reasons.append(f"recipe status {recipe['status']} is not executable")
        if recipe["status"] == "deprecated_supported" and not explicitly_requested:
            reasons.append(f"deprecated; superseded by {recipe['superseded_by']['id']}@{recipe['superseded_by']['version']} (select explicitly to use)")
        if scope.get("lob") and scope["lob"] not in app["lob"]:
            reasons.append(f"lob {scope['lob']} outside {app['lob']}")
            unsupported.append(f"lob={scope['lob']}")
        if scope.get("state") and scope["state"] not in app["state"]:
            reasons.append(f"state {scope['state']} outside {app['state']}")
            unsupported.append(f"state={scope['state']}")
        if rec.intent.window and rec.intent.window.value is not None:
            lo, hi = app["window_value"]["min"], app["window_value"]["max"]
            if not lo <= rec.intent.window.value <= hi:
                reasons.append(f"window {rec.intent.window.value} outside [{lo},{hi}]")
                unsupported.append(f"window={rec.intent.window.value}")
        for d in rec.intent.dimensions:
            if d not in recipe["allowed_dimensions"]:
                reasons.append(f"dimension {d} not supported by the certified recipe")
                unsupported.append(f"dimension={d}")
        return {"recipe": f"{recipe['id']}@{recipe['version']}", "applicable": not reasons, "reasons": reasons,
                "unsupported": unsupported}

    # ---- evidence selection (greedy constrained set cover) ----------------
    def select_evidence(self, manifest: ObligationManifest, rec: IntentRecord, recipe: dict[str, Any] | None,
                        budget: int = 8) -> dict[str, Any]:
        needed = {o.id for o in manifest.obligations.values()
                  for s in o.satisfied_by if s.startswith("evidence:")}
        claim_needed = {}
        for o in manifest.obligations.values():
            for s in o.satisfied_by:
                if s.startswith("evidence:"):
                    claim_needed.setdefault(s.split(":", 1)[1], set()).add(o.id)
        cutoff = rec.intent.knowledge_cutoff
        lob = rec.intent.scope.get("lob")
        pool = []
        rejected = []
        for frag in self.registry.evidence:
            why = None
            if frag["visibility"] not in self.visibility:
                why = "not visible to requester metadata access"
            elif recipe and frag["claim_id"] not in recipe["evidence_claims"]:
                why = "not declared by the resolved recipe"
            elif lob and frag["scope"].get("lob") and lob not in frag["scope"]["lob"]:
                why = "outside scope"
            elif cutoff and (frag["valid_from"] > cutoff or (frag["valid_to"] and frag["valid_to"] < cutoff)):
                why = "not valid at the knowledge cutoff"
            elif frag["contradictions"]:
                why = "contradicted"
            if why:
                rejected.append({"claim_id": frag["claim_id"], "reason": why})
            else:
                pool.append(frag)
        uncovered = set(needed)
        chosen: list[dict[str, Any]] = []
        while uncovered and len(chosen) < budget:
            best = max(pool, key=lambda f: len(set(claim_needed.get(f["claim_id"], ())) & uncovered), default=None)
            if best is None or not set(claim_needed.get(best["claim_id"], ())) & uncovered:
                break
            chosen.append(best)
            uncovered -= set(claim_needed.get(best["claim_id"], ()))
            pool.remove(best)
        entries = []
        for frag in chosen:
            for ob_id in sorted(claim_needed.get(frag["claim_id"], ())):
                entries.append({
                    "obligation_id": ob_id, "claim_id": frag["claim_id"], "source_locator": frag["source_locator"],
                    "source_hash": "sha256:" + sha256_hex(frag["claim"]), "version": frag["version"],
                    "authority_class": frag["authority"], "scope": frag["scope"], "packet": frag["packet"],
                    "knowledge_timestamp": frag["valid_from"], "applicable_business_time": [frag["valid_from"], frag["valid_to"]],
                    "retrieval_reason": f"covers {ob_id}", "contradiction_status": "none",
                })
        return {"selected_claims": [f["claim_id"] for f in chosen], "entries": entries,
                "missing_obligations": sorted(uncovered), "rejected_fragments": rejected,
                "packets_consulted": sorted({p for f in chosen for p in f["packets"]}),
                "selection": "greedy_set_cover", "budget": budget}

    # ---- main ---------------------------------------------------------------
    def ground(self, rec: IntentRecord) -> Resolution:
        cands = self.candidate_recipes(rec)
        requested = rec.intent.requested_recipe
        decisions = [self.applicability(c["recipe"], rec, explicitly_requested=(c["recipe"]["id"] == requested)) | {"matched_by": c["matched_by"], "similarity": c["similarity"]}
                     for c in cands]
        applicable = [c["recipe"] for c, d in zip(cands, decisions) if d["applicable"]]
        if requested and any(r["id"] == requested for r in applicable):
            applicable = [r for r in applicable if r["id"] == requested]
        unsupported = sorted({u for d in decisions for u in d["unsupported"]})
        metric_types = sorted({r["metric_type"] for r in applicable})
        domains = rec.intent.domains or ["quote", "policy"]
        manifest = self.compiler.pass_a(metric_types, applicable, domains)

        res = Resolution(status="resolved", route=None, candidates=[{"recipe": f"{c['recipe']['id']}@{c['recipe']['version']}",
                         "matched_by": c["matched_by"], "similarity": c["similarity"]} for c in cands],
                         applicability=decisions, unsupported=unsupported, obligations_a=manifest)

        if not cands or (not applicable and not unsupported):
            metric = rec.intent.metric or "unrecognized"
            res.status = "unsupported" if rec.intent.metric else "missing_definition"
            res.gaps.append(f"No certified recipe resolves metric '{metric}'. Use the Recipe builder to draft one; no source execution will occur.")
            manifest.evaluate({"route_resolved": False}, metadata_only=True)
            return res
        if not applicable:
            res.status = "unsupported"
            res.gaps.append("Requested meaning is outside every certified recipe: " + ", ".join(unsupported))
            manifest.evaluate({"route_resolved": False}, metadata_only=True)
            return res

        # different meanings must not be cost-ranked into a hidden definition choice
        distinct = {(r["id"], r["version"]) for r in applicable}
        if len(distinct) > 1:
            res.questions.append(Clarification(
                id="q_definition", field="requested_recipe",
                question="More than one certified definition applies. Which conversion definition do you mean?",
                options=[f"{r['id']}" for r in applicable], recommended=applicable[0]["id"]))
            res.status = "clarification_required"
            manifest.evaluate({"route_resolved": False}, metadata_only=True)
            return res

        recipe = applicable[0]
        res.route = {"recipe_id": recipe["id"], "version": recipe["version"], "binding_version": recipe["binding_version"],
                     "certificate_ref": recipe["certification"]["certificate_ref"], "status": recipe["status"],
                     "title": recipe["title"]}
        defs = recipe["definitions"]
        tz = defs["business_timezone"]

        # material questions for missing parameters
        scope = rec.intent.scope
        if not scope.get("state"):
            res.questions.append(Clarification(id="q_state", field="scope.state", question="Which state?", options=recipe["applicability"]["state"], recommended="NC"))
        if not scope.get("lob"):
            res.questions.append(Clarification(id="q_lob", field="scope.lob", question="Which line of business?", options=recipe["applicability"]["lob"], recommended="personal_auto"))
        if rec.intent.cohort is None:
            res.questions.append(Clarification(id="q_cohort", field="cohort", question="Which quote cohort (local dates, end exclusive)?",
                                               options=[], recommended={"start": "2026-08-01", "end_exclusive": "2026-09-01"}))
        if rec.intent.window is None or rec.intent.window.value is None:
            res.questions.append(Clarification(id="q_window", field="window.value", question="Conversion window length in days?", options=[7, 30, 60], recommended=30))
        if not rec.intent.knowledge_cutoff:
            recommended = None
            if self.clock is not None:
                local_today = self.clock.now().astimezone(ZoneInfo(tz)).date()
                recommended = end_of_local_day_utc((local_today - timedelta(days=1)).isoformat(), tz)
            res.questions.append(Clarification(id="q_cutoff", field="knowledge_cutoff", question="Knowledge cutoff (what was known by when)?",
                                               options=[], recommended=recommended))
        if res.questions:
            res.status = "clarification_required"

        # bind recipe definitions (recipe-grounded proposal for confirmation)
        window_val = rec.intent.window.value if rec.intent.window else None
        bound = {
            "metric": recipe["id"], "population": defs["population"], "quote_identity": defs["quote_identity"],
            "attribution_rule": defs["attribution_rule"], "bind_definition": defs["bind_definition"],
            "cancellation_treatment": defs["cancellation_treatment"], "business_timezone": tz,
            "window": {"value": window_val, "unit": defs["window"]["unit"], "interval": defs["window"]["interval"],
                       "anchor": defs["window"]["anchor"], "end_rule": defs["window"]["end_rule"], "dst_rule_id": defs["window"]["dst_rule_id"]},
            "time_basis": defs["time_basis"], "maturity_policy": defs["maturity_policy"], "domains": recipe["domains"],
            "output_grain": list(rec.intent.dimensions),
            "resolved_recipes": [self.registry.recipe_ref(recipe)],
        }
        res.bound_fields = bound
        ev = self.select_evidence(manifest, rec, recipe)
        res.evidence_manifest = ev
        products_ok = all(self.registry.port(p)["authority"] == "authoritative" for p in recipe["input_ports"])
        manifest.evaluate({"route_resolved": True, "evidence": ev["selected_claims"], "products_ok": products_ok,
                           "policies": set(self.registry.policies)}, metadata_only=True)
        blockers = manifest.metadata_blocking()
        if blockers:
            res.status = "missing_definition"
            res.missing = [b.id for b in blockers]
            res.gaps.extend(f"No authoritative evidence satisfies {b.id}: {b.description}" for b in blockers)
        return res


def dimension_words() -> dict[str, list[str]]:
    return KNOWN_DIMENSIONS
