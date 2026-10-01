"""Skill invocation with schema validation, guards, fallback and receipts."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..canonical import sha256_hex, stable_json
from .guards import Guard, passthrough
from .providers import LLMProvider, OfflineProvider, ProviderError
from .registry import Skill, load_skills
from .schema import validate

AGENT_DDL = """
CREATE TABLE IF NOT EXISTS agent_invocations (invocation_id TEXT PRIMARY KEY, skill TEXT, skill_version TEXT, provider TEXT,
  model TEXT, request_id TEXT, execution_id TEXT, prompt_hash TEXT, context_hash TEXT, response_hash TEXT, inputs_json TEXT,
  output_json TEXT, accepted INTEGER, fallback_used INTEGER, fallback_reason TEXT, rejected_json TEXT, notes_json TEXT,
  latency_ms REAL, at TEXT);
CREATE INDEX IF NOT EXISTS ix_agent_skill ON agent_invocations(skill, prompt_hash);
"""


def prompt_hash(skill: Skill, inputs: dict[str, Any]) -> str:
    return "sha256:" + sha256_hex(skill.name + "@" + skill.version + "\n" + stable_json(inputs))


@dataclass
class SkillResult:
    skill: str
    output: dict[str, Any]
    provider: str
    model: str | None
    fallback_used: bool
    rejected: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    invocation_id: str = ""
    prompt_hash: str = ""

    def receipt(self) -> dict[str, Any]:
        return {"invocation_id": self.invocation_id, "skill": self.skill, "provider": self.provider, "model": self.model,
                "fallback_used": self.fallback_used, "rejected": self.rejected, "notes": self.notes, "prompt_hash": self.prompt_hash}


class AgentRunner:
    def __init__(self, store, trace, clock, provider: LLMProvider | None = None):
        self.store = store
        self.trace = trace
        self.clock = clock
        self.provider = provider or OfflineProvider()
        self.offline = OfflineProvider()
        self.skills = load_skills()
        store._con().executescript(AGENT_DDL)

    def invoke(self, skill_name: str, inputs: dict[str, Any], *, guard: Guard | None = None, request_id: str | None = None,
               execution_id: str | None = None) -> SkillResult:
        skill = self.skills[skill_name]
        guard = guard or passthrough
        inputs = json.loads(stable_json(inputs))  # JSON-normalized, hashable
        ph = prompt_hash(skill, inputs)
        notes: list[str] = []
        fallback_reason = None
        comp = None
        t0 = time.perf_counter()
        try:
            comp = self.provider.complete(skill, inputs)
        except ProviderError as exc:
            fallback_reason = f"provider error: {exc}"
        g = None
        if comp is not None:
            errs = validate(comp.output, skill.schema)
            if errs:
                fallback_reason = "schema violations: " + "; ".join(errs[:4])
            else:
                g = guard(comp.output)
                if not g.usable:
                    fallback_reason = "guard rejected proposal: " + "; ".join(r.get("reason", "") for r in g.rejected)
        rejected_from_model = g.rejected if (g and not g.usable) else []
        if fallback_reason is not None:
            notes.append(fallback_reason)
            model_provider = comp.provider if comp else self.provider.name
            comp = self.offline.complete(skill, inputs)
            errs = validate(comp.output, skill.schema)
            if errs:
                raise RuntimeError(f"offline {skill_name} violates its schema: {errs}")
            g = guard(comp.output)
            if not g.usable:
                raise RuntimeError(f"offline {skill_name} rejected by guard: {g.rejected}")
            notes.append(f"fell back from {model_provider} to offline engine")
        result = SkillResult(skill=skill_name, output=g.output, provider=comp.provider, model=comp.model,
                             fallback_used=fallback_reason is not None, rejected=rejected_from_model + g.rejected,
                             notes=notes + comp.notes + g.notes, invocation_id="inv_" + uuid.uuid4().hex[:12], prompt_hash=ph)
        latency = round((time.perf_counter() - t0) * 1000, 3)
        self.store.x("INSERT INTO agent_invocations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            result.invocation_id, skill.name, skill.version, comp.provider, comp.model, request_id, execution_id, ph,
            skill.context_hash, "sha256:" + sha256_hex(comp.raw_text), stable_json(inputs), stable_json(g.output), 1,
            int(result.fallback_used), fallback_reason, stable_json(result.rejected), stable_json(result.notes), latency,
            self.clock.now_z()))
        if request_id and self.trace is not None:
            self.trace.span(request_id, f"agent:{skill_name}", execution_id=execution_id, status="proposed",
                            actor={"type": "agent", "version": f"{skill.name}@{skill.version}", "provider": comp.provider, "model": comp.model},
                            decision={"rationale": str(g.output.get("rationale", ""))[:280], "rejected": len(result.rejected),
                                      "fallback_used": result.fallback_used},
                            input_refs={"prompt_hash": ph, "context_hash": skill.context_hash},
                            output_refs={"invocation_id": result.invocation_id})
        return result

    def invocations(self, request_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if request_id:
            rows = self.store.q("SELECT * FROM agent_invocations WHERE request_id=? ORDER BY at, rowid", (request_id,))
        else:
            rows = self.store.q("SELECT * FROM agent_invocations ORDER BY rowid DESC LIMIT ?", (limit,))
        return rows
