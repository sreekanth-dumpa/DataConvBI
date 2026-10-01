"""Feedback context lifecycle (section 17).

Each case is stored separately from the main specifications under
``feedback/<case_id>/`` (assessment.md, case.json, proposed patch, regression
results, promotion decision). States: proposed, evaluated, approved, active,
superseded, rejected. Approval as *advisory* context never activates
executable logic; retrieval is scoped by recipe/domain and applicability.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATES = ("proposed", "evaluated", "approved", "active", "superseded", "rejected")
TRANSITIONS = {
    "proposed": {"evaluated", "rejected", "approved"},
    "evaluated": {"approved", "rejected"},
    "approved": {"active", "superseded", "rejected"},
    "active": {"superseded"},
    "superseded": set(),
    "rejected": set(),
}


class FeedbackDirectory:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _case_path(self, case_id: str) -> Path:
        return self.root / case_id / "case.json"

    def load(self, case_id: str) -> dict[str, Any]:
        return json.loads(self._case_path(case_id).read_text())

    def save(self, case: dict[str, Any]) -> None:
        p = self._case_path(case["case_id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(case, indent=2, sort_keys=True, default=str))

    def all(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.root.glob("*/case.json")):
            try:
                out.append(json.loads(p.read_text()))
            except json.JSONDecodeError:
                continue
        return out

    def transition(self, case_id: str, new_state: str, actor: str, note: str = "", executable: bool = False) -> dict[str, Any]:
        case = self.load(case_id)
        cur = case["promotion"]["state"]
        if new_state not in TRANSITIONS[cur]:
            raise ValueError(f"illegal transition {cur} -> {new_state}")
        if new_state == "active" and case["promotion"].get("change_class") in ("semantic_specification", "operator", "compiler"):
            if not case["promotion"].get("independent_regression_passed"):
                raise ValueError("executable/semantic changes require independent cases, holdout regression and review before activation")
        case["promotion"]["state"] = new_state
        case["promotion"].setdefault("history", []).append({"to": new_state, "actor": actor, "note": note})
        case["promotion"]["advisory_only"] = not executable
        self.save(case)
        return case

    def retire_stale(self, compiler_version: str, registry_versions: dict[str, str]) -> list[str]:
        """Decay: physical-plan advice is superseded when the compiler or registries it was measured under change."""
        retired = []
        for c in self.all():
            if c["promotion"]["state"] not in ("approved", "active"):
                continue
            v = c.get("versions") or {}
            if c["promotion"].get("change_class") in ("physical_plan", "none", "presentation") and v and (
                    v.get("compiler") != compiler_version or v.get("registries") != registry_versions):
                c["promotion"]["state"] = "superseded"
                c["promotion"].setdefault("history", []).append({"to": "superseded", "actor": "decay",
                                                                 "note": "compiler or registry versions changed"})
                self.save(c)
                retired.append(c["case_id"])
        return retired

    def retrieve(self, recipe_id: str, domains: list[str], scope: dict[str, Any]) -> list[dict[str, Any]]:
        """Return approved/active advisory cases applicable to this request only."""
        out = []
        for c in self.all():
            if c["promotion"]["state"] not in ("approved", "active"):
                continue
            app = c.get("applicability", {})
            if app.get("recipe_id") and app["recipe_id"] != recipe_id:
                continue
            if app.get("domains") and not set(app["domains"]) & set(domains):
                continue
            if app.get("state") and scope.get("state") and app["state"] != scope["state"]:
                continue
            if app.get("lob") and scope.get("lob") and app["lob"] != scope["lob"]:
                continue
            out.append({"case_id": c["case_id"], "recommendations": c.get("recommendations", [])[:3],
                        "change_class": c["promotion"].get("change_class"), "advisory_only": c["promotion"].get("advisory_only", True)})
        return out
