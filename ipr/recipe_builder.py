"""Recipe builder (section 18).

Turns natural-language logic into a versioned *draft* recipe by asking
questions selected from unresolved semantic obligations. A generated draft is
never certified by being generated: certification needs owner review,
independently approved expected outcomes, a source mapping and a registry
publication workflow. The builder never accepts or runs user-provided SQL.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUESTIONS = [
    {"id": "identity", "obligation": "ob.identity", "question": "What is one opportunity (the denominator unit)?",
     "options": {"submission": "A submission opportunity counted once across all quote branches/versions",
                 "branch": "Each quote branch (PolicyPeriod version) counted independently"}},
    {"id": "event", "obligation": "ob.event_definition", "question": "Which event counts as a conversion?",
     "options": {"bind_any_branch": "First committed bind on any linked submission branch",
                 "bind_same_branch": "Committed bind on the same quote branch",
                 "policy_issued": "Policy issuance (not a bind)"}},
    {"id": "population", "obligation": "ob.attribution", "question": "Which quotes are eligible and where are dimensions attributed?",
     "options": {"first_eligible": "First eligible quoted branch; dimensions from that branch",
                 "every_quoted": "Every quoted branch; dimensions from its own branch"}},
    {"id": "window", "obligation": "ob.timezone_window", "question": "How is the conversion window measured?",
     "options": {"local_half_open": "Local calendar days in the business timezone, [anchor, anchor + N days)",
                 "utc_inclusive": "UTC dates, quote date through quote date + N inclusive",
                 "elapsed_hours": "Elapsed hours from the quote timestamp"}},
    {"id": "cancellation", "obligation": "ob.cancellation", "question": "Does a later cancellation reverse a conversion?",
     "options": {"gross": "No - gross conversion is not reversed",
                 "net_within_window": "Yes - cancelled within the window is not a conversion"}},
    {"id": "maturity", "obligation": "ob.maturity", "question": "How are opportunities without a complete window treated?",
     "options": {"exclude": "Exclude from the denominator and report them",
                 "count_unconverted": "Count as not converted"}},
]

INSTALLED = {
    ("gross_submission_quote_conversion", "2.0.0"): {"identity": "submission", "event": "bind_any_branch", "population": "first_eligible",
                                                     "window": "local_half_open", "cancellation": "gross", "maturity": "exclude"},
    ("quote_conversion", "1.0.0"): {"identity": "branch", "event": "bind_same_branch", "population": "every_quoted",
                                    "window": "utc_inclusive", "cancellation": "gross", "maturity": "exclude"},
}

ADVERSARIAL = [
    "several quote iterations (branches) for one submission",
    "several binds for one opportunity",
    "bind exactly at the window end and one second before",
    "bind outside the window",
    "late-arriving bind known after the cutoff",
    "immature quotes near the cohort end",
    "unmatched bind (no linked quote branch)",
    "overlapping policy versions",
    "cancellation after a bind",
    "late-evening local quote that falls on a different UTC date",
    "DST transition inside the window",
    "equal quote timestamps requiring a tie-break",
]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:48] or "draft"


class RecipeBuilder:
    def __init__(self, drafts_dir: Path):
        self.dir = Path(drafts_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def questions(self, answered: dict[str, str] | None = None) -> list[dict[str, Any]]:
        answered = answered or {}
        return [q for q in QUESTIONS if q["id"] not in answered]

    @staticmethod
    def match_installed(answers: dict[str, str]) -> dict[str, Any]:
        for (rid, ver), spec in INSTALLED.items():
            if all(answers.get(k) == v for k, v in spec.items()):
                return {"matches": True, "recipe": {"id": rid, "version": ver},
                        "note": "Answers are equivalent to an installed certified algebra; use it instead of a new recipe."}
        unsupported = []
        for q in QUESTIONS:
            a = answers.get(q["id"])
            if a is None:
                unsupported.append(f"{q['id']}: unanswered")
            elif all(spec.get(q["id"]) != a for spec in INSTALLED.values()):
                unsupported.append(f"{q['id']}={a}: no installed operator implements this meaning")
        return {"matches": False, "recipe": None, "unsupported_meanings": unsupported,
                "note": "Draft requires new operators/bindings, owner-approved expected outcomes and certification."}

    def build_draft(self, logic: str, answers: dict[str, str], *, title: str, owner: str, examples: list[str] | None = None,
                    dimensions: list[str] | None = None, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        missing = [q["id"] for q in QUESTIONS if q["id"] not in answers]
        draft_id = _slug(title)
        folder = self.dir / draft_id
        folder.mkdir(parents=True, exist_ok=True)
        version = len(list(folder.glob("v*.json"))) + 1
        match = self.match_installed(answers)
        draft = {
            "id": draft_id, "version": f"0.{version}.0-draft", "title": title, "owner": owner, "status": "draft",
            "certification": None, "certification_requirements": [
                "owner review of definitions", "independently approved expected outcomes for positive and adversarial examples",
                "certified source mapping/binding per product", "held-out regression", "registry publication workflow"],
            "logic": logic, "examples": examples or [], "scope": scope or {},
            "clarifications": [{"id": q["id"], "obligation": q["obligation"], "question": q["question"],
                                "answer": answers.get(q["id"]), "answer_text": q["options"].get(answers.get(q["id"], ""), None)}
                               for q in QUESTIONS],
            "unresolved_obligations": missing,
            "definitions": {k: answers.get(k) for k in [q["id"] for q in QUESTIONS]},
            "output_grain": dimensions or [],
            "sufficient_statistics": {"eligible_all": "count", "eligible_mature": "count", "converted": "count"},
            "determinism": "deterministic_exact",
            "adversarial_examples": [{"case": c, "expected_outcome": None, "approved_by": None} for c in ADVERSARIAL],
            "installed_algebra_match": match,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "draft_ref": str(folder / f"v{version}.json"),
            "draft_uid": uuid.uuid4().hex[:8],
        }
        (folder / f"v{version}.json").write_text(json.dumps(draft, indent=2, sort_keys=True))
        return draft

    def drafts(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text()) for p in sorted(self.dir.glob("*/v*.json"))]
