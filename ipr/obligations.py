"""Compiler-derived semantic obligations (section 5).

Pass A: union of metric-template obligations, candidate recipe declarations and
domain/access/temporal policy, closed over registry implications. An
unresolved metric type yields classification and definition-resolution
obligations — never an empty set.

Pass B: Pass A plus obligations declared by every IR operator, edge, product
binding, materialization and output contract of the logical/physical plan,
closed again. A missing operator declaration is itself a blocking obligation.

Agents may propose additional obligations; removals, waivers or claims of
satisfaction by an agent are rejected and recorded.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .canonical import fingerprint
from .registry import Registry

METADATA_SOURCES = ("evidence:", "registry:", "route", "policy:")


@dataclass
class Obligation:
    id: str
    description: str
    severity: str = "mandatory"
    scopes: list[str] = field(default_factory=list)
    origins: list[dict[str, str]] = field(default_factory=list)
    satisfied_by: list[str] = field(default_factory=list)
    validation: str | None = None
    depends_on: list[str] = field(default_factory=list)
    status: str = "open"  # open | satisfied | pending_validation | validated | failed | unsatisfied
    evidence: list[str] = field(default_factory=list)
    added_by: str = "compiler"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ObligationManifest:
    pass_name: str
    registry_versions: dict[str, str]
    obligations: dict[str, Obligation] = field(default_factory=dict)
    rejected_agent_actions: list[dict[str, Any]] = field(default_factory=list)
    frozen_hash: str | None = None

    # ---- construction ---------------------------------------------------
    def add(self, ob_id: str, registry: Registry, origin: dict[str, str], scope: str = "request",
            severity: str = "mandatory", added_by: str = "compiler") -> Obligation:
        spec = registry.obligations.get(ob_id)
        if ob_id in self.obligations:
            ob = self.obligations[ob_id]
            if origin not in ob.origins:
                ob.origins.append(origin)
            if scope not in ob.scopes:
                ob.scopes.append(scope)
            if severity == "mandatory":
                ob.severity = "mandatory"
            return ob
        if spec is None:
            ob = Obligation(id=ob_id, description=f"Undeclared obligation {ob_id}", severity=severity, scopes=[scope],
                            origins=[origin], satisfied_by=[], status="unsatisfied", added_by=added_by,
                            reason="obligation not present in the versioned registry")
        else:
            ob = Obligation(id=ob_id, description=spec["description"], severity=severity, scopes=[scope], origins=[origin],
                            satisfied_by=list(spec.get("satisfied_by", [])), validation=spec.get("validation"),
                            depends_on=list(spec.get("implies", [])), added_by=added_by)
        self.obligations[ob_id] = ob
        return ob

    def close(self, registry: Registry) -> None:
        """Transitive closure over registry ``implies`` dependencies."""
        frontier = list(self.obligations.values())
        while frontier:
            ob = frontier.pop()
            for dep in ob.depends_on:
                if dep not in self.obligations:
                    new = self.add(dep, registry, {"kind": "implied_by", "ref": ob.id, "version": registry.versions["obligations"]},
                                   scope=ob.scopes[0] if ob.scopes else "request", severity=ob.severity)
                    frontier.append(new)

    # ---- agent proposals --------------------------------------------------
    def merge_agent_proposal(self, registry: Registry, add: Iterable[str] = (), remove: Iterable[str] = (),
                             waive: Iterable[str] = (), mark_satisfied: Iterable[str] = (), agent: str = "agent") -> None:
        for ob_id in add:
            self.add(ob_id, registry, {"kind": "agent_proposal", "ref": agent, "version": "n/a"}, added_by=agent)
        for action, ids in (("remove", remove), ("waive", waive), ("mark_satisfied", mark_satisfied)):
            for ob_id in ids:
                self.rejected_agent_actions.append({"action": action, "obligation": ob_id, "agent": agent,
                                                    "reason": "agents cannot remove, waive or satisfy compiler-derived obligations"})
        self.close(registry)

    # ---- satisfaction -------------------------------------------------------
    def evaluate(self, context: dict[str, Any], metadata_only: bool = False) -> None:
        """Mark satisfaction from a context of available facts.

        context keys: evidence (set of claim ids), route_resolved (bool),
        products_ok (bool), access_decision (bool), cut_admitted (bool),
        certificate (set of obligation ids certified by the plan certificate),
        runtime (set of runtime capability names), compiler_checks (set),
        validated (dict assertion -> bool).
        """
        evidence = set(context.get("evidence", ()))
        certified = set(context.get("certificate", ()))
        runtime_caps = set(context.get("runtime", ()))
        compiler_checks = set(context.get("compiler_checks", ()))
        policies = set(context.get("policies", ()))
        validated = context.get("validated", {})
        for ob in self.obligations.values():
            if ob.status == "unsatisfied" and not ob.satisfied_by:
                continue  # undeclared / missing declaration: stays blocking
            missing: list[str] = []
            sources: list[str] = []
            deferred: list[str] = []
            for src in ob.satisfied_by:
                if metadata_only and not src.startswith(METADATA_SOURCES):
                    deferred.append(src)
                    continue
                ok = self._check(src, ob, context, evidence, certified, runtime_caps, compiler_checks, policies)
                (sources if ok else missing).append(src)
            ob.evidence = sources
            if missing:
                ob.status = "unsatisfied"
                ob.reason = "missing: " + ", ".join(missing)
            elif deferred:
                ob.status = "open"
                ob.reason = "deferred to resolution/planning: " + ", ".join(deferred)
            elif ob.validation:
                verdict = validated.get(ob.validation)
                if verdict is True:
                    ob.status = "validated"
                    ob.reason = None
                elif verdict is False:
                    ob.status = "failed"
                    ob.reason = f"validation {ob.validation} failed"
                else:
                    ob.status = "pending_validation"
                    ob.reason = f"requires runtime validation {ob.validation}"
            else:
                ob.status = "satisfied"
                ob.reason = None

    @staticmethod
    def _check(src, ob, context, evidence, certified, runtime_caps, compiler_checks, policies) -> bool:
        if src.startswith("evidence:"):
            return src.split(":", 1)[1] in evidence
        if src == "route":
            return bool(context.get("route_resolved"))
        if src.startswith("registry:"):
            return bool(context.get("products_ok"))
        if src.startswith("policy:"):
            return src.split(":", 1)[1] in policies
        if src == "access_decision":
            return bool(context.get("access_decision"))
        if src == "cut_admission":
            return bool(context.get("cut_admitted"))
        if src == "certificate":
            return ob.id in certified
        if src.startswith("runtime:"):
            return src.split(":", 1)[1] in runtime_caps
        if src.startswith("compiler:"):
            return src.split(":", 1)[1] in compiler_checks
        return False

    def blocking(self) -> list[Obligation]:
        return [o for o in self.obligations.values() if o.severity == "mandatory" and o.status in ("unsatisfied", "failed")]

    def metadata_blocking(self) -> list[Obligation]:
        return [o for o in self.blocking() if all(s.startswith(METADATA_SOURCES) for s in o.satisfied_by) or not o.satisfied_by]

    def freeze(self) -> str:
        self.frozen_hash = fingerprint(self.to_dict(include_hash=False))
        return self.frozen_hash

    def to_dict(self, include_hash: bool = True) -> dict[str, Any]:
        d = {"pass": self.pass_name, "registry_versions": self.registry_versions,
             "obligations": {k: v.to_dict() for k, v in sorted(self.obligations.items())},
             "rejected_agent_actions": self.rejected_agent_actions}
        if include_hash:
            d["frozen_hash"] = self.frozen_hash
        return d

    def copy(self, pass_name: str) -> "ObligationManifest":
        m = copy.deepcopy(self)
        m.pass_name = pass_name
        m.frozen_hash = None
        return m


class ObligationCompiler:
    def __init__(self, registry: Registry):
        self.registry = registry

    def pass_a(self, metric_types: list[str], recipes: list[dict[str, Any]], domains: list[str]) -> ObligationManifest:
        r = self.registry
        m = ObligationManifest("A", dict(r.versions))
        types = [t for t in metric_types if t in r.templates] or ["__unresolved__"]
        for t in types:
            for ob in r.templates[t]["obligations"]:
                m.add(ob, r, {"kind": "metric_template", "ref": t, "version": r.versions["templates"]})
        if not recipes:
            for ob in r.templates["__unresolved__"]["obligations"]:
                m.add(ob, r, {"kind": "metric_template", "ref": "__unresolved__", "version": r.versions["templates"]})
        for rec in recipes:
            for ob in rec.get("obligations", []):
                m.add(ob, r, {"kind": "recipe", "ref": f"{rec['id']}@{rec['version']}", "version": rec["version"]})
        for pid, pol in r.policies.items():
            doms = pol.get("applies_to_domains", [])
            if "*" in doms or set(doms) & set(domains):
                for ob in pol.get("obligations", []):
                    m.add(ob, r, {"kind": "policy", "ref": pid, "version": r.versions["policies"]})
        m.close(r)
        return m

    def pass_b(self, manifest_a: ObligationManifest, logical: dict[str, Any], physical: dict[str, Any] | None = None) -> ObligationManifest:
        r = self.registry
        m = manifest_a.copy("B")
        nodes = {n["id"]: n for n in logical["nodes"]}
        for node in logical["nodes"]:
            op = node["operator"]
            decl = r.operators.get(op)
            scope = f"node:{node['id']}"
            if decl is None:
                ob = m.add(f"ob.operator_declaration_missing:{op}", r, {"kind": "operator", "ref": op, "version": "missing"}, scope=scope)
                ob.status = "unsatisfied"
                ob.reason = f"operator {op} has no registry declaration"
                continue
            for ob in decl.get("obligations", []):
                m.add(ob, r, {"kind": "operator", "ref": f"{op}/v{decl['version']}", "version": r.versions["operators"]}, scope=scope)
        for edge in logical.get("edges", []):
            src, dst = nodes.get(edge["from"]), nodes.get(edge["to"])
            m.add("ob.edge_grain_compatible", r, {"kind": "edge", "ref": f"{edge['from']}->{edge['to']}", "version": r.versions["operators"]},
                  scope=f"edge:{edge['from']}->{edge['to']}")
            if src is None or dst is None:
                ob = m.add(f"ob.edge_dangling:{edge['from']}->{edge['to']}", r, {"kind": "edge", "ref": "dangling", "version": "n/a"})
                ob.status = "unsatisfied"
        for port in logical.get("bindings", []):
            m.add("ob.contract_version_bound", r, {"kind": "binding", "ref": port, "version": r.versions["products"]}, scope=f"port:{port}")
            m.add("ob.port_authorized", r, {"kind": "binding", "ref": port, "version": r.versions["products"]}, scope=f"port:{port}")
        for out in logical.get("outputs", []):
            for ob in r.operators[out["operator"]]["obligations"]:
                m.add(ob, r, {"kind": "output_contract", "ref": out["operator"], "version": r.versions["operators"]}, scope="output")
        if physical is not None:
            for pn in physical["nodes"]:
                if pn.get("materialization") == "stage":
                    for ob in r.operators["materialize_stage"]["obligations"]:
                        m.add(ob, r, {"kind": "materialization", "ref": pn["id"], "version": r.versions["operators"]}, scope=f"stage:{pn['id']}")
                if pn.get("operator") and pn["operator"] not in r.operators:
                    ob = m.add(f"ob.operator_declaration_missing:{pn['operator']}", r,
                               {"kind": "physical_operator", "ref": pn["operator"], "version": "missing"}, scope=f"node:{pn['id']}")
                    ob.status = "unsatisfied"
                elif pn.get("operator"):
                    for ob in r.operators[pn["operator"]]["obligations"]:
                        m.add(ob, r, {"kind": "physical_operator", "ref": pn["operator"], "version": r.versions["operators"]}, scope=f"node:{pn['id']}")
        m.close(r)
        return m
