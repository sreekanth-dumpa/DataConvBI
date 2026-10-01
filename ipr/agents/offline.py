"""Deterministic offline engine: one implementation per skill.

Used when no model provider is configured, and as the fallback whenever a
model output fails schema validation or its guard. Outputs follow the same
schemas as the model so every downstream consumer is provider-agnostic.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from ..intent import KNOWN_DIMENSIONS, rule_based_interpret

TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set[str]:
    return set(TOKEN.findall(s.lower()))


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


# ----------------------------------------------------------------------------- interpret
def intent_interpreter(inp: dict[str, Any]) -> dict[str, Any]:
    body, assumptions, _ = rule_based_interpret(inp["text"])
    cohort = body.get("cohort") or {}
    vocab = set(inp.get("dimension_vocabulary") or KNOWN_DIMENSIONS)
    dims = [d for d in body.get("dimensions", []) if d in vocab]
    unknown = [d for d in body.get("dimensions", []) if d not in vocab]
    return {
        "metric": body.get("metric"), "state": (body.get("scope") or {}).get("state"), "lob": (body.get("scope") or {}).get("lob"),
        "cohort_start": cohort.get("start"), "cohort_end_exclusive": cohort.get("end_exclusive"),
        "window_days": (body.get("window") or {}).get("value"),
        "knowledge_cutoff_local_date": body.get("knowledge_cutoff_local_date"), "dimensions": dims, "unknown_dimensions": unknown,
        "requested_definition": body.get("requested_recipe"), "cut_mode": (body.get("cut_request") or {}).get("mode", "pinned"),
        "assumptions": assumptions, "rationale": "Pattern interpreter matched explicit state, LOB, month, window, cutoff and 'by' phrases.",
    }


# ----------------------------------------------------------------------------- ground
def recipe_nominator(inp: dict[str, Any]) -> dict[str, Any]:
    q = inp["question"].lower()
    qt = _tokens(q)
    noms = []
    for r in inp["catalog"]:
        text = " ".join([r["title"], r["description"], " ".join(r["aliases"])])
        rt = _tokens(text)
        jac = len(qt & rt) / len(qt | rt) if qt and rt else 0.0
        alias = any(a in q for a in r["aliases"])
        score = min(1.0, jac * 2 + (0.5 if alias else 0.0))
        if score >= 0.25:
            why = "alias match in the question" if alias else "shared business vocabulary with the recipe description"
            noms.append({"recipe_id": r["recipe_id"], "version": r["version"], "relevance": round(score, 3),
                         "reason": f"{why}: {', '.join(sorted(qt & rt)[:6])}"})
    noms.sort(key=lambda n: -n["relevance"])
    return {"nominations": noms, "rationale": "Lexical relevance over recipe titles, descriptions and aliases."}


FIELD_QUESTIONS = {
    "scope.state": "Which state's quotes should be included?",
    "scope.lob": "Which line of business?",
    "cohort": "Which quotes form the cohort? (first eligible quote date range, local time)",
    "window.value": "How many days after the first eligible quote may a bind occur to count as converted?",
    "knowledge_cutoff": "As of when should the data be known? (later records are excluded)",
    "requested_recipe": "Which conversion definition do you mean?",
}


def clarification_writer(inp: dict[str, Any]) -> dict[str, Any]:
    qs = []
    for q in inp["questions"]:
        labels = [str(o).replace("_", " ") for o in q.get("options", [])]
        qs.append({"id": q["id"], "business_question": FIELD_QUESTIONS.get(q["field"], q["question"]), "option_labels": labels})
    i = inp["intent"]
    parts = []
    if i.get("population"):
        parts.append(f"Population: {str(i['population']).replace('_', ' ')}.")
    qi = i.get("quote_identity") or {}
    if qi:
        parts.append(f"Each {str(qi.get('business_unit', 'opportunity')).replace('_', ' ')} is counted "
                     f"{'once across quote branches' if qi.get('multi_version_policy') == 'count_once_across_branches' else 'per quote branch'}.")
    if i.get("bind_definition"):
        parts.append(f"Conversion means {str(i['bind_definition']).replace('_', ' ')}; {str(i.get('cancellation_treatment', '')).replace('_', ' ')}.")
    w = i.get("window") or {}
    if w.get("value"):
        parts.append(f"Window: {w['value']} {str(w.get('unit', '')).replace('_', ' ')}s, interval {w.get('interval')}, in {i.get('business_timezone')}.")
    if i.get("maturity_policy"):
        parts.append(f"Maturity: {str(i['maturity_policy']).replace('_', ' ')}.")
    if i.get("knowledge_cutoff"):
        parts.append(f"Knowledge cutoff: {i['knowledge_cutoff']}.")
    if i.get("cohort"):
        parts.append(f"Cohort: {i['cohort']['start']} to {i['cohort']['end_exclusive']} (end exclusive).")
    parts.append(f"Grouped by: {', '.join(i.get('dimensions') or []) or 'no dimensions'}.")
    return {"questions": qs, "confirmation_summary": " ".join(parts),
            "rationale": "Templated restatement of the bound recipe definitions; no new meaning added."}


# ----------------------------------------------------------------------------- plan
def obligation_proposer(inp: dict[str, Any]) -> dict[str, Any]:
    present = {o["id"] for o in inp["manifest"]}
    registry = set(inp["registry_obligations"])
    add = []
    # obligations declared by the plan's logical operators that Pass A does not yet carry; they apply to
    # every physical variant (variant-specific obligations come from the compiler's Pass B, not the agent)
    for n in inp["plan_nodes"]:
        for ob in inp.get("operator_obligations", {}).get(n["operator"], []):
            if ob not in present and ob not in {a["obligation_id"] for a in add}:
                add.append({"obligation_id": ob, "reason": f"declared by {n['operator']} at node {n['id']}"})
    add = [a for a in add if a["obligation_id"] in registry]
    ev = []
    if inp.get("identity") == "submission":
        ev.append("Validate the deployed Job/PolicyPeriod-to-submission mapping for the bound product contract version")
    return {"add": add, "remove": [], "evidence_requests": ev,
            "rationale": "Checked operator coverage of grain, attribution and partition-merge risks."}


CANONICAL_STEPS = [
    ("scoped_scan", "port", "quote_branch_id"), ("canonical_event_selection", "quote_branch_id", "opportunity_id"),
    ("key_reduction", "bind_event_id", "bind_event_id"), ("window_exists", "opportunity_id", "opportunity_id"),
    ("aggregate_sufficient_stats", "opportunity_id", "output_dimensions"),
]


def plan_proposer(inp: dict[str, Any]) -> dict[str, Any]:
    s = inp["stats"]
    admissible = [c["variant"] for c in inp["candidates"] if c["admissible"]]
    cohort = s.get("cohort_rows") or 0
    fan = s.get("bind_multiplicity_max") or 1
    if (cohort > 20000 or fan > 50) and "partitioned" in admissible:
        pref, why = "partitioned", f"cohort {cohort} rows / max fan-out {fan}: co-partition before the window reduction"
    elif cohort < 5000 and "fused" in admissible:
        pref, why = "fused", f"small cohort ({cohort} rows): staging overhead outweighs observability benefit"
    else:
        pref = "staged" if "staged" in admissible else admissible[0]
        why = "moderate volume: materialize cohort and linked binds to validate grain and enable checkpoints"
    return {"logical_steps": [{"operator": o, "input_grain": a, "output_grain": b} for o, a, b in CANONICAL_STEPS],
            "preferred_variant": pref, "variant_reason": why,
            "materialization_boundaries": ["canonical_event_selection", "key_reduction"] if pref != "fused" else [],
            "python_operators": [{"operator_id": "wilson_interval", "confidence_level": 0.95,
                                  "reason": "report sampling uncertainty with every conversion rate"}]
            if "wilson_interval" in {o["operator_id"] for o in inp.get("approved_python_operators", [])} else [],
            "rationale": "Canonical cohort, key-reduced linked binds, exists-window outcome, additive aggregation."}


def probe_advisor(inp: dict[str, Any]) -> dict[str, Any]:
    order = ["cohort_size", "bind_multiplicity", "linkage_coverage", "heavy_hitters", "duplicate_canonical_key", "key_sample"]
    unc = inp.get("uncertainty", 1)
    ranked = []
    for name in sorted((p["name"] for p in inp["probes"]), key=lambda n: order.index(n) if n in order else 99):
        change = unc >= 2
        ranked.append({"probe": name, "could_change_decision": change,
                       "reason": ("registered estimates are approximate (uncertainty x%.1f); result can move the variant choice" % unc)
                       if change else "statistics already exact for this cut"})
    return {"ranked": ranked, "rationale": "Volume and fan-out probes first: they decide partitioning and materialization."}


# ----------------------------------------------------------------------------- present
def answer_narrator(inp: dict[str, Any]) -> dict[str, Any]:
    f = inp["facts"]
    t = f["totals"]
    dims = f["dimensions"]
    claims = [{"text": _pct(t["conversion_rate"]), "fact_ref": "totals.conversion_rate"},
              {"text": f"{t['converted']:,}", "fact_ref": "totals.converted"},
              {"text": f"{t['eligible_mature']:,}", "fact_ref": "totals.eligible_mature"},
              {"text": f"{t['immature']:,}", "fact_ref": "totals.immature"},
              {"text": f"{t['quality_excluded']:,}", "fact_ref": "totals.quality_excluded"}]
    headline = f"{_pct(t['conversion_rate'])} of mature opportunities converted ({t['converted']:,} of {t['eligible_mature']:,})."
    sents = [headline,
             f"{t['immature']:,} opportunities were excluded as immature and {t['quality_excluded']:,} as quality exclusions."]
    rows = [r for r in f["by_dimension"] if r["conversion_rate"] is not None]
    if len(rows) >= 2:
        hi = max(range(len(f["by_dimension"])), key=lambda i: f["by_dimension"][i]["conversion_rate"] or -1)
        lo = min(range(len(f["by_dimension"])), key=lambda i: f["by_dimension"][i]["conversion_rate"] if f["by_dimension"][i]["conversion_rate"] is not None else 2)
        lab = lambda r: " / ".join(str(r[d]) for d in dims)  # noqa: E731
        rh, rl = f["by_dimension"][hi], f["by_dimension"][lo]
        sents.append(f"The highest observed rate was {lab(rh)} at {_pct(rh['conversion_rate'])} and the lowest {lab(rl)} at "
                     f"{_pct(rl['conversion_rate'])}; these are associations, not causal effects.")
        claims += [{"text": _pct(rh["conversion_rate"]), "fact_ref": f"by_dimension[{hi}].conversion_rate"},
                   {"text": _pct(rl["conversion_rate"]), "fact_ref": f"by_dimension[{lo}].conversion_rate"}]
    if any("imbalance" in c.lower() for c in inp.get("caveats", [])):
        sents.append("Immature-exclusion rates differ substantially between groups, so comparisons may be biased.")
    chart = ({"type": "grouped_bar", "x": dims[0], "color": dims[1], "reason": "two dimensions: grouped bars by the second"}
             if len(dims) >= 2 else {"type": "bar", "x": dims[0] if dims else None, "color": None, "reason": "single comparison axis"})
    return {"headline": headline, "narrative": " ".join(sents), "claims": claims, "chart_recommendation": chart,
            "rationale": "Generated from validated answer facts only."}


# ----------------------------------------------------------------------------- assess
def assessment_diagnostician(inp: dict[str, Any]) -> dict[str, Any]:
    causes = []
    for gate, v in inp["gates"].items():
        if v == "fail":
            causes.append({"cause": f"gate {gate} failed", "evidence_metric": gate})
    m = inp["metrics"]
    eta = m.get("eta_calibration") or {}
    if eta.get("within_p90") is False:
        causes.append({"cause": "duration model under-estimates this workload class", "evidence_metric": "eta_calibration"})
    for node, q in (m.get("cardinality_calibration") or {}).items():
        if (q or {}).get("value") and q["value"] > 3:
            causes.append({"cause": f"cardinality estimate off at {node}", "evidence_metric": "cardinality_calibration"})
            break
    recs = [{"change_class": r["change_class"], "recommendation": r["recommendation"], "evidence": r["evidence"]}
            for r in inp.get("rule_recommendations", [])]
    return {"likely_causes": causes, "recommendations": recs or [{"change_class": "none", "recommendation": "No change indicated",
                                                                  "evidence": "gates"}],
            "rationale": "Mapped failed gates and calibration metrics to change classes."}


BYPASS = ("skip validation", "ignore access", "bypass", "without permission", "other states", "all states", "disable check",
          "turn off", "skip the check", "don't check", "ignore the policy")


def feedback_classifier(inp: dict[str, Any]) -> dict[str, Any]:
    t = (inp.get("feedback") or "").lower()
    rules = [("physical_plan", ("slow", "expensive", "cost", "took too long", "timeout")),
             ("semantic_specification", ("wrong", "definition", "should count", "should include", "should exclude", "incorrect")),
             ("context", ("missing rule", "no evidence", "source", "mapping")),
             ("presentation", ("chart", "confusing", "label", "highlight", "explain", "unclear", "show"))]
    cls = next((c for c, kws in rules if any(k in t for k in kws)), "none")
    dims = [d for d in inp.get("dimensions", []) if d.replace("_", " ") in t or d in t]
    sentiment = "negative" if any(k in t for k in ("wrong", "bad", "slow", "confusing", "incorrect")) else \
        ("positive" if any(k in t for k in ("good", "great", "plausible", "thanks", "correct")) else "neutral")
    return {"change_class": cls, "scope_dimensions": dims, "sentiment": sentiment,
            "requests_control_bypass": any(k in t for k in BYPASS),
            "summary": (inp.get("feedback") or "no feedback")[:160], "rationale": "Keyword classification of the feedback text."}


# ----------------------------------------------------------------------------- recipe builder
DRAFT_RULES = {
    "identity": [("submission", ("per submission", "count once", "once per submission", "submission")),
                 ("branch", ("per quote", "each quote", "each branch", "every version"))],
    "event": [("policy_issued", ("issued", "issuance")), ("bind_same_branch", ("same quote", "same branch")),
              ("bind_any_branch", ("bind", "bound"))],
    "population": [("first_eligible", ("first quote", "first eligible", "first quoted")),
                   ("every_quoted", ("every quote", "all quotes", "each quote"))],
    "window": [("elapsed_hours", ("hours",)), ("utc_inclusive", ("utc", "inclusive")),
               ("local_half_open", ("local", "calendar day", "within", "days"))],
    "cancellation": [("net_within_window", ("excluding", "cancelled in", "net", "minus cancel", "not cancelled")),
                     ("gross", ("gross", "regardless of cancel", "even if cancelled"))],
    "maturity": [("count_unconverted", ("count as not converted", "treat as unconverted")),
                 ("exclude", ("exclude immature", "complete window", "mature"))],
}


def recipe_drafter(inp: dict[str, Any]) -> dict[str, Any]:
    t = (inp.get("logic") or "").lower() + " " + " ".join(inp.get("examples") or []).lower()
    answers, unresolved = [], []
    for q in inp["questions"]:
        hit = None
        for option, kws in DRAFT_RULES.get(q["id"], []):
            k = next((k for k in kws if k in t), None)
            if k and option in q["options"]:
                hit = (option, k)
                break
        if hit:
            answers.append({"question_id": q["id"], "option": hit[0], "evidence": f"phrase '{hit[1]}'"})
        else:
            unresolved.append(q["id"])
    title = " ".join(w.capitalize() for w in TOKEN.findall(inp.get("logic", ""))[:5]) or "Draft recipe"
    return {"answers": answers, "unresolved": unresolved, "title": title,
            "rationale": "Answered only questions whose option is named by a phrase in the logic."}


# ----------------------------------------------------------------------------- route
ROUTE_HINTS = {
    "feedback-classifier": ("answer", "result", "chart", "wrong", "slow", "confusing", "thanks", "great", "should have", "looks",
                            "incorrect", "bypass", "feedback"),
    "recipe-drafter": ("define", "definition", "new metric", "recipe", "rule:", "counted as", "should count as", "metric logic",
                       "share of", "build a"),
    "intent-interpreter": ("what", "how many", "show", "conversion for", "rate for", "?", "compare", "by product", "by channel"),
}


def skill_router(inp: dict[str, Any]) -> dict[str, Any]:
    t = inp["message"].lower()
    entry = inp["entry_points"]
    descs = {c["name"]: c["description"].lower() for c in inp["catalog"] if c["name"] in entry}
    mt = _tokens(t)
    scores = {}
    for name in entry:
        hint = sum(1 for h in ROUTE_HINTS.get(name, ()) if h in t)
        overlap = len(mt & _tokens(descs.get(name, ""))) / max(1, len(mt))
        scores[name] = hint + overlap
    if inp.get("has_answer") is False:
        scores["feedback-classifier"] = scores.get("feedback-classifier", 0) - 2
    best = max(scores, key=lambda k: (scores[k], k == "intent-interpreter"))
    total = sum(max(v, 0) for v in scores.values()) or 1
    return {"skill": best, "confidence": round(max(scores[best], 0) / total, 3),
            "reason": f"message matches the description of {best}",
            "rationale": "Scored entry-point descriptions and routing cues against the message."}


OFFLINE: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "skill-router": skill_router,
    "intent-interpreter": intent_interpreter, "recipe-nominator": recipe_nominator, "clarification-writer": clarification_writer,
    "obligation-proposer": obligation_proposer, "plan-proposer": plan_proposer, "probe-advisor": probe_advisor,
    "answer-narrator": answer_narrator, "assessment-diagnostician": assessment_diagnostician,
    "feedback-classifier": feedback_classifier, "recipe-drafter": recipe_drafter,
}
