"""Provider-neutral intent proposer interface with an optional Claude adapter.

The model only *proposes* a draft intent body (section 7: interpretation and
recipe questions). Grounding, obligations, compilation and certification stay
deterministic; a proposal is validated against the same field whitelist as the
rule-based interpreter. The receipt stores prompt/context/response hashes and
provider/model identifiers so accepted IR can be replayed without asking the
model to reinterpret the request. Any adapter error falls back to the
deterministic interpreter and is recorded in the receipt.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol

from .canonical import sha256_hex, stable_json
from .intent import InterpretationReceipt, rule_based_interpret

ALLOWED_KEYS = {"metric", "scope", "cohort", "window", "knowledge_cutoff", "knowledge_cutoff_local_date", "dimensions",
                "requested_recipe", "cut_request"}

SYSTEM_PROMPT = """You convert an insurance analytics question into a draft intent JSON object.
Only extract what the user said; leave a field out when it is not stated. Never invent definitions.
Allowed keys:
- metric: "quote_conversion" for quote-to-bind / conversion questions, otherwise a short snake_case metric name
- scope: {"state": two-letter US state code, "lob": "personal_auto" | "homeowners"}
- cohort: {"start": "YYYY-MM-DD", "end_exclusive": "YYYY-MM-DD"} (local dates)
- window: {"value": integer days}
- knowledge_cutoff_local_date: "YYYY-MM-DD" when the user says "as of"/"known by" a date
- dimensions: list drawn from product_version, channel, driver_age, household, territory
- requested_recipe: "quote_conversion" only if the user explicitly asks for the legacy per-quote definition
- cut_request: {"mode": "latest"} only if the user asks for latest/provisional data
Respond with the JSON object only."""


class IntentProposer(Protocol):
    name: str

    def propose(self, text: str) -> tuple[dict[str, Any], list[str], InterpretationReceipt]: ...


class RuleBasedProposer:
    name = "rule_based"

    def propose(self, text: str):
        return rule_based_interpret(text)


def _sanitize(body: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in body.items() if k in ALLOWED_KEYS}
    out.setdefault("dimensions", [])
    if not isinstance(out["dimensions"], list):
        out["dimensions"] = []
    return out


class ClaudeProposer:
    """Optional adapter using the Anthropic Python SDK (``pip install anthropic``)."""

    name = "claude"

    def __init__(self, model: str = "claude-opus-5-5", client=None):
        import anthropic  # optional dependency

        self._anthropic = anthropic
        self.client = client or anthropic.Anthropic()
        self.model = model

    def propose(self, text: str):
        fallback_body, fallback_assumptions, _ = rule_based_interpret(text)
        notes: list[str] = []
        body: dict[str, Any] | None = None
        response_text = ""
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": text}],
            )
            if response.stop_reason == "refusal":
                notes.append("model declined; deterministic interpreter used")
            else:
                response_text = "".join(b.text for b in response.content if b.type == "text")
                match = re.search(r"\{.*\}", response_text, re.S)
                if match:
                    body = _sanitize(json.loads(match.group(0)))
                else:
                    notes.append("no JSON object in response; deterministic interpreter used")
        except json.JSONDecodeError:
            notes.append("invalid JSON from model; deterministic interpreter used")
        except self._anthropic.APIConnectionError:
            notes.append("network error; deterministic interpreter used")
        except self._anthropic.APIStatusError as exc:
            notes.append(f"API error {exc.status_code}; deterministic interpreter used")
        if body is None:
            body = fallback_body
        receipt = InterpretationReceipt(
            interpreter="claude" if not notes else "claude_fallback_rule_based", version="1",
            prompt_hash=sha256_hex(text), context_hash=sha256_hex(SYSTEM_PROMPT), response_hash=sha256_hex(response_text or stable_json(body)),
            model=self.model, provider="anthropic", notes=notes or ["proposal validated against the intent field whitelist"])
        return body, fallback_assumptions, receipt


def default_proposer() -> IntentProposer:
    if os.environ.get("IPR_LLM", "").lower() in ("claude", "anthropic"):
        try:
            return ClaudeProposer(model=os.environ.get("IPR_LLM_MODEL", "claude-opus-5-5"))
        except ImportError:
            pass
    return RuleBasedProposer()
