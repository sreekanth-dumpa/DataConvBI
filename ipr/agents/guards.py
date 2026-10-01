"""Deterministic guards: what an agent proposal may change, checked in code.

A guard returns the accepted (possibly filtered) output, the rejected parts
with reasons, and whether the proposal as a whole is usable. If unusable, the
runner falls back to the offline engine (whose output must pass the guard).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from ..canonical import stable_json

BYPASS_WORDS = ("bypass", "skip validation", "ignore access", "disable", "without certification", "waive")


@dataclass
class GuardResult:
    usable: bool
    output: dict[str, Any]
    rejected: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


Guard = Callable[[dict[str, Any]], GuardResult]


def _iso(d: str | None) -> bool:
    if d is None:
        return True
    try:
        date.fromisoformat(d)
        return True
    except ValueError:
        return False


def interpreter_guard(vocabulary: list[str]) -> Guard:
    def g(o: dict[str, Any]) -> GuardResult:
        out, rej = dict(o), []
        for k in ("cohort_start", "cohort_end_exclusive", "knowledge_cutoff_local_date"):
            if not _iso(out.get(k)):
                rej.append({"field": k, "value": out[k], "reason": "not an ISO date"})
                out[k] = None
        if out.get("state") is not None and not re.fullmatch(r"[A-Z]{2}", str(out["state"])):
            rej.append({"field": "state", "value": out["state"], "reason": "not a state code"})
            out["state"] = None
        if out.get("lob") not in (None, "personal_auto", "homeowners"):
            rej.append({"field": "lob", "value": out["lob"], "reason": "unknown line of business"})
            out["lob"] = None
        if out.get("window_days") is not None and not 1 <= int(out["window_days"]) <= 365:
            rej.append({"field": "window_days", "value": out["window_days"], "reason": "outside 1..365"})
            out["window_days"] = None
        if out.get("requested_definition") not in (None, "quote_conversion"):
            rej.append({"field": "requested_definition", "value": out["requested_definition"], "reason": "not a registered legacy definition"})
            out["requested_definition"] = None
        # requested breakdowns outside the vocabulary stay visible (unsupported meaning), never silently dropped
        unknown = list(out.get("unknown_dimensions") or []) + [d for d in out.get("dimensions", []) if d not in vocabulary]
        out["dimensions"] = [d for d in out.get("dimensions", []) if d in vocabulary]
        out["unknown_dimensions"] = sorted(set(unknown))
        return GuardResult(True, out, rej)
    return g


def nomination_guard(catalog: dict[str, str]) -> Guard:
    def g(o: dict[str, Any]) -> GuardResult:
        keep, rej = [], []
        for n in o["nominations"]:
            if catalog.get(n["recipe_id"]) == n["version"]:
                keep.append({**n, "relevance": max(0.0, min(1.0, float(n["relevance"])))})
            else:
                rej.append({"nomination": n, "reason": "recipe/version not in the registered catalog"})
        return GuardResult(True, {**o, "nominations": keep}, rej, ["nominations are candidates only; hard applicability rules decide"])
    return g


def _numbers(text: str) -> list[str]:
    # standalone numbers (not part of identifiers such as PV2025)
    return [m.group(0).replace(",", "") for m in re.finditer(r"(?<![A-Za-z0-9_])\d[\d,]*(?:\.\d+)?%?(?![A-Za-z_])", text)]


def clarification_guard(question_ids: list[str], option_counts: dict[str, int], intent: dict[str, Any]) -> Guard:
    allowed = set(_numbers(stable_json(intent)))
    allowed |= {n for v in re.findall(r"\d{4}-\d{2}-\d{2}", stable_json(intent)) for n in v.split("-")}

    def g(o: dict[str, Any]) -> GuardResult:
        rej = []
        ids = [q["id"] for q in o["questions"]]
        if ids != question_ids:
            return GuardResult(False, o, [{"reason": f"question ids {ids} differ from {question_ids}"}])
        for q in o["questions"]:
            if option_counts.get(q["id"], 0) and len(q["option_labels"]) != option_counts[q["id"]]:
                return GuardResult(False, o, [{"reason": f"option count changed for {q['id']}"}])
        extra = [n for n in _numbers(o["confirmation_summary"]) if n.rstrip("%") not in allowed and n not in allowed]
        if extra:
            return GuardResult(False, o, [{"reason": f"summary states numbers not in the bound intent: {extra}"}])
        return GuardResult(True, o, rej)
    return g


def obligation_guard(registry_ids: set[str]) -> Guard:
    def g(o: dict[str, Any]) -> GuardResult:
        rej = [{"action": "remove", "obligation": r, "reason": "agents cannot remove obligations"} for r in o.get("remove", [])]
        add = []
        for a in o.get("add", []):
            if a["obligation_id"] in registry_ids:
                add.append(a)
            else:
                rej.append({"action": "add", "obligation": a["obligation_id"], "reason": "not a registered obligation id"})
        return GuardResult(True, {**o, "add": add}, rej)
    return g


def plan_guard(approved_ops: set[str], compiled_sequence: list[str], admissible: list[str], frontier: list[str],
               python_ops: dict[str, dict[str, Any]] | None = None) -> Guard:
    python_ops = python_ops or {}

    def g(o: dict[str, Any]) -> GuardResult:
        rej, notes = [], []
        keep_py = []
        for p in o.get("python_operators", []):
            spec = python_ops.get(p["operator_id"])
            rule = (spec or {}).get("params", {}).get("confidence_level")
            if spec is None:
                rej.append({"python_operator": p, "reason": "not an approved Python operator"})
            elif rule and not rule["min"] <= p["confidence_level"] <= rule["max"]:
                rej.append({"python_operator": p, "reason": "parameter outside declared bounds"})
            else:
                keep_py.append(p)
        o = {**o, "python_operators": keep_py}
        ops = [s["operator"] for s in o["logical_steps"]]
        bad = [x for x in ops if x not in approved_ops]
        if bad:
            return GuardResult(False, o, [{"reason": f"unapproved operators {bad}"}])
        core = [x for x in ops if x != "scoped_scan"]
        expected = [x for x in compiled_sequence if x != "scoped_scan"]
        out = dict(o)
        if core != expected:
            rej.append({"reason": "semantic_delta: proposed operator path differs from the certified composition",
                        "proposed": core, "certified": expected})
            out["semantic_delta"] = True
        else:
            out["semantic_delta"] = False
        if o["preferred_variant"] not in admissible:
            rej.append({"reason": f"variant {o['preferred_variant']} is not admissible"})
            out["preference_honoured"] = False
        elif o["preferred_variant"] not in frontier:
            rej.append({"reason": f"variant {o['preferred_variant']} is dominated (outside the Pareto frontier)"})
            out["preference_honoured"] = False
        else:
            out["preference_honoured"] = not out["semantic_delta"]
        notes.append("compiler-synthesized IR remains authoritative; preference only breaks ties within the frontier")
        return GuardResult(True, out, rej, notes)
    return g


def probe_guard(names: set[str]) -> Guard:
    def g(o: dict[str, Any]) -> GuardResult:
        keep = [r for r in o["ranked"] if r["probe"] in names]
        rej = [{"probe": r["probe"], "reason": "not an approved probe"} for r in o["ranked"] if r["probe"] not in names]
        seen = {r["probe"] for r in keep}
        keep += [{"probe": n, "could_change_decision": True, "reason": "not ranked by agent; kept by policy"} for n in sorted(names - seen)]
        return GuardResult(True, {**o, "ranked": keep}, rej)
    return g


def _resolve(facts: dict[str, Any], ref: str) -> Any:
    cur: Any = facts
    for part in re.findall(r"[a-z_]+|\[\d+\]", ref):
        if part.startswith("["):
            cur = cur[int(part[1:-1])]
        else:
            cur = cur[part]
    return cur


def _allowed_numbers(facts: dict[str, Any]) -> set[str]:
    allowed: set[str] = set()

    def add_val(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, int):
            allowed.add(str(v))
        elif isinstance(v, float):
            allowed.add(f"{v * 100:.1f}%")
            allowed.add(f"{v * 100:.0f}%")
            allowed.add(f"{v:.3f}".rstrip("0").rstrip("."))
    for k, v in facts["totals"].items():
        add_val(v)
    for r in facts["by_dimension"]:
        for k, v in r.items():
            add_val(v)
    if facts.get("follow_up_imbalance") is not None:
        add_val(facts["follow_up_imbalance"])
    w = facts.get("window") or {}
    if w.get("value"):
        allowed.add(str(w["value"]))
    for s in (facts.get("knowledge_cutoff") or "", stable_json(facts.get("cohort") or {})):
        allowed |= set(re.findall(r"\d+", s))
    allowed.add(str(len(facts["by_dimension"])))
    return allowed


def narration_guard(facts: dict[str, Any]) -> Guard:
    allowed = _allowed_numbers(facts)
    dims = facts["dimensions"]

    def g(o: dict[str, Any]) -> GuardResult:
        unverified = [n for n in _numbers(o["headline"] + " " + o["narrative"]) if n not in allowed]
        if unverified:
            return GuardResult(False, o, [{"reason": f"numbers not traceable to validated facts: {unverified[:6]}"}])
        for c in o["claims"]:
            try:
                v = _resolve(facts, c["fact_ref"])
            except (KeyError, IndexError, ValueError, TypeError):
                return GuardResult(False, o, [{"reason": f"claim ref {c['fact_ref']} does not resolve"}])
            text = c["text"].replace(",", "")
            ok = (isinstance(v, int) and text == str(v)) or (isinstance(v, float) and text in (f"{v * 100:.1f}%", f"{v * 100:.0f}%"))
            if not ok:
                return GuardResult(False, o, [{"reason": f"claim {c['text']!r} does not match {c['fact_ref']}={v!r}"}])
        if re.search(r"\b(caused|causes|causing|because of|led to|drove|driven by|due to|resulted in)\b", o["headline"] + " " + o["narrative"], re.I):
            return GuardResult(False, o, [{"reason": "causal language is not permitted for observed associations"}])
        out = dict(o)
        ch = o["chart_recommendation"]
        rej = []
        if ch["type"] != "table" and (ch["x"] not in dims or (ch["color"] is not None and ch["color"] not in dims)
                                      or (ch["type"] == "grouped_bar" and len(dims) < 2)):
            rej.append({"chart": ch, "reason": "chart fields must be validated output dimensions"})
            out["chart_recommendation"] = {"type": "table", "x": None, "color": None, "reason": "fallback: invalid recommendation"}
        return GuardResult(True, out, rej, [f"{len(o['claims'])} claims verified against facts"])
    return g


def diagnosis_guard(metric_keys: set[str]) -> Guard:
    def g(o: dict[str, Any]) -> GuardResult:
        rej = []
        causes = [c for c in o["likely_causes"] if c["evidence_metric"] in metric_keys]
        rej += [{"cause": c, "reason": "evidence metric not in the assessment"} for c in o["likely_causes"] if c not in causes]
        recs = []
        for r in o["recommendations"]:
            if any(w in r["recommendation"].lower() for w in BYPASS_WORDS):
                rej.append({"recommendation": r, "reason": "recommends bypassing controls"})
            else:
                recs.append(r)
        return GuardResult(True, {**o, "likely_causes": causes, "recommendations": recs}, rej,
                           ["gates, metrics and expected outputs are not modifiable by the diagnostician"])
    return g


def drafter_guard(options: dict[str, list[str]]) -> Guard:
    def g(o: dict[str, Any]) -> GuardResult:
        keep, rej = [], []
        for a in o["answers"]:
            if a["option"] in options.get(a["question_id"], []):
                keep.append(a)
            else:
                rej.append({"answer": a, "reason": "option not offered for this question"})
        unresolved = sorted(set(o["unresolved"]) | (set(options) - {a["question_id"] for a in keep}))
        return GuardResult(True, {**o, "answers": keep, "unresolved": unresolved}, rej, ["proposals pre-fill the form; the user decides"])
    return g


def passthrough(o: dict[str, Any]) -> GuardResult:
    return GuardResult(True, o)
