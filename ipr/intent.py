"""Intent record (section 4), semantic payload and canonical semantic hash.

A draft is incomplete. Confirmation binds to the hash of the resolved semantic
payload, which includes recipe/binding versions and all resolved material
clarifications, and excludes raw prose, budgets, presentation form, source cut
and access identity (those have separate fingerprints). Any semantic edit
invalidates the approved hash.
"""

from __future__ import annotations

import copy
import re
import uuid
from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .canonical import fingerprint, normalize_semantic, sha256_hex, stable_json

SEMANTIC_SCHEMA_VERSION = "intent-semantic/v2"


class Requester(BaseModel):
    subject: str
    tenant: str = "farmers"
    purpose: str = "pricing_review"


class Window(BaseModel):
    value: int = 30
    unit: str = "local_calendar_day"
    interval: str = "[start,end)"
    anchor: str = "first_eligible_quote_generated_at"
    end_rule: str = "same_local_time_plus_n_calendar_days"
    dst_rule_id: Optional[str] = "domain_calendar_v1"


class Cohort(BaseModel):
    start: str
    end_exclusive: str


class CutRequest(BaseModel):
    mode: Literal["pinned", "latest"] = "pinned"
    selection: str = "last_complete_publication_covering_cutoff"


class Output(BaseModel):
    form: str = "assessment_with_chart"
    row_limit: int = 10000


class IntentBody(BaseModel):
    domains: list[str] = Field(default_factory=lambda: ["quote", "policy"])
    metric: Optional[str] = None
    population: Optional[str] = None
    scope: dict[str, str] = Field(default_factory=dict)
    cohort: Optional[Cohort] = None
    quote_identity: Optional[dict[str, Any]] = None
    attribution_rule: Optional[dict[str, Any]] = None
    bind_definition: Optional[str] = None
    cancellation_treatment: Optional[str] = None
    business_timezone: Optional[str] = None
    window: Optional[Window] = None
    time_basis: Optional[str] = None
    knowledge_cutoff: Optional[str] = None
    cut_request: CutRequest = Field(default_factory=CutRequest)
    maturity_policy: Optional[str] = None
    dimensions: list[str] = Field(default_factory=list)
    output_grain: list[str] = Field(default_factory=list)
    output: Output = Field(default_factory=Output)
    resolved_recipes: list[dict[str, Any]] = Field(default_factory=list)
    requested_recipe: Optional[str] = None  # explicit user choice of a definition


class Constraints(BaseModel):
    discovery_target_credits: float = 0.10
    execution_target_credits: float = 2.0
    max_runtime_seconds: int = 900
    max_plan_revisions: int = 3
    queue_allowed: bool = True


class Clarification(BaseModel):
    id: str
    field: str
    question: str
    options: list[Any] = Field(default_factory=list)
    recommended: Any = None
    answer: Any = None
    material: bool = True
    status: Literal["open", "resolved", "dismissed"] = "open"


class Confirmation(BaseModel):
    status: Literal["pending", "confirmed", "invalidated"] = "pending"
    approved_semantic_hash: Optional[str] = None
    actor: Optional[str] = None
    at: Optional[str] = None


class IntentRecord(BaseModel):
    request_id: str
    conversation_id: str
    parent_request_id: Optional[str] = None
    raw_request_ref: Optional[str] = None
    raw_text: str = ""
    requester: Requester
    intent: IntentBody
    constraints: Constraints = Field(default_factory=Constraints)
    budget_policy_id: str = "warehouse_admission_v2"
    assumptions: list[str] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)
    confirmation: Confirmation = Field(default_factory=Confirmation)
    version: int = 1


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:10]


# ---------------------------------------------------------------------------
# Semantic payload and hash
# ---------------------------------------------------------------------------

def semantic_payload(rec: IntentRecord) -> dict[str, Any]:
    body = rec.intent.model_dump(mode="json")
    body.pop("output", None)          # presentation form is not meaning
    body.pop("requested_recipe", None)  # captured by resolved_recipes
    resolved = sorted(
        ({"field": c.field, "answer": c.answer} for c in rec.clarifications if c.status == "resolved" and c.material),
        key=lambda c: stable_json(c))
    return {"schema": SEMANTIC_SCHEMA_VERSION, "intent": body, "resolved_clarifications": resolved}


def semantic_hash(rec: IntentRecord) -> str:
    return fingerprint(semantic_payload(rec))


def coverage_payload(rec: IntentRecord) -> dict[str, Any]:
    """Semantic payload without output dimensions: the reuse coverage key."""
    p = semantic_payload(rec)
    p = copy.deepcopy(p)
    p["intent"].pop("dimensions", None)
    p["intent"].pop("output_grain", None)
    p["resolved_clarifications"] = [c for c in p["resolved_clarifications"] if c["field"] not in ("dimensions",)]
    return p


def coverage_key(rec: IntentRecord) -> str:
    return fingerprint(coverage_payload(rec))


def confirmation_matches(rec: IntentRecord) -> bool:
    return rec.confirmation.status == "confirmed" and rec.confirmation.approved_semantic_hash == semantic_hash(rec)


def set_path(obj: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        if cur.get(p) is None:
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def apply_edits(rec: IntentRecord, edits: dict[str, Any]) -> tuple[IntentRecord, bool]:
    """Apply field edits (dotted paths under ``intent``). Returns (new record, semantic_changed)."""
    before = semantic_hash(rec)
    data = rec.model_dump(mode="json")
    for path, value in edits.items():
        set_path(data["intent"], path, value)
    new = IntentRecord.model_validate(data)
    changed = semantic_hash(new) != before
    new.version = rec.version + 1
    if changed and new.confirmation.status == "confirmed":
        new.confirmation = Confirmation(status="invalidated")
    return new, changed


# ---------------------------------------------------------------------------
# Rule-based interpreter (deterministic form). LLM proposals use ipr.llm.
# ---------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                       "september", "october", "november", "december"], start=1)}
STATES = {"NC": "north carolina", "SC": "south carolina", "VA": "virginia", "GA": "georgia", "TX": "texas", "CA": "california"}
LOBS = {"personal_auto": ["personal auto", "auto", "pa "], "homeowners": ["homeowners", "home owner", "homeowner", "ho3"]}
KNOWN_DIMENSIONS = {
    "product_version": ["product version", "product_version", "rate version", "product"],
    "channel": ["channel", "distribution channel"],
    "driver_age": ["driver age", "driver_age", "age band"],
    "household": ["household"],
    "territory": ["territory", "zip"],
}


class InterpretationReceipt(BaseModel):
    interpreter: str
    version: str
    prompt_hash: str
    context_hash: str
    response_hash: str
    model: Optional[str] = None
    provider: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = date(year, month, 1)
    end = date(year + (month == 12), (month % 12) + 1, 1)
    return start.isoformat(), end.isoformat()


def rule_based_interpret(text: str) -> tuple[dict[str, Any], list[str], InterpretationReceipt]:
    """Propose a draft intent body from text. Unrecognized meaning stays empty."""
    t = " " + text.lower() + " "
    body: dict[str, Any] = {"dimensions": []}
    assumptions: list[str] = []
    if re.search(r"conver|bind|bound|close rate", t):
        body["metric"] = "quote_conversion"
    elif re.search(r"retention|renewal|loss ratio|frequency|severity", t):
        body["metric"] = re.search(r"retention|renewal|loss ratio|frequency|severity", t).group(0).replace(" ", "_")
    if "legacy" in t or "per quote branch" in t or "per-branch" in t:
        body["requested_recipe"] = "quote_conversion"
    for code, name in STATES.items():
        if re.search(rf"\b{code.lower()}\b", t) or name in t:
            body.setdefault("scope", {})["state"] = code
            break
    for lob, words in LOBS.items():
        if any(w in t for w in words):
            body.setdefault("scope", {})["lob"] = lob
            break
    m = re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})", t)
    if m:
        s, e = _month_bounds(int(m.group(2)), MONTHS[m.group(1)])
        body["cohort"] = {"start": s, "end_exclusive": e}
    m = re.search(r"from\s+(\d{4}-\d{2}-\d{2})\s+(?:to|until|through)\s+(\d{4}-\d{2}-\d{2})", t)
    if m:
        body["cohort"] = {"start": m.group(1), "end_exclusive": m.group(2)}
        assumptions.append("Cohort end date interpreted as exclusive")
    m = re.search(r"(\d{1,3})[\s-]*day", t)
    if m:
        body["window"] = {"value": int(m.group(1))}
    m = re.search(r"(?:as of|cutoff|known by)\s+(\d{4}-\d{2}-\d{2})(?:t(\d{2}:\d{2}:\d{2})z?)?", t)
    if m:
        body["knowledge_cutoff_local_date"] = m.group(1)
        if m.group(2):
            body["knowledge_cutoff"] = f"{m.group(1)}T{m.group(2)}Z"
    if "latest" in t and "pinned" not in t:
        body["cut_request"] = {"mode": "latest", "selection": "latest_available_publication"}
    by = re.search(r"\b(?:by|per|split by|broken down by)\s+(.+?)(?:\s+for\s|\s+in\s|\s+as of|\s+with|[.?!]|$)", t)
    if by:
        seg = by.group(1)
        for dim, words in KNOWN_DIMENSIONS.items():
            if any(w in seg for w in words):
                body["dimensions"].append(dim)
    if not body["dimensions"]:
        for dim, words in KNOWN_DIMENSIONS.items():
            if any(f" {w} " in t for w in words) and dim in ("product_version", "channel"):
                body["dimensions"].append(dim)
    receipt = InterpretationReceipt(
        interpreter="rule_based", version="1", prompt_hash=sha256_hex(text), context_hash=sha256_hex("rule_based/1"),
        response_hash=sha256_hex(stable_json(body)), notes=["deterministic pattern interpreter; no model invoked"])
    return body, assumptions, receipt


def normalize_semantic_view(rec: IntentRecord) -> dict[str, Any]:
    return normalize_semantic(semantic_payload(rec))
