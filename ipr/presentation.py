"""Presenting the result (section 15).

Structured answer facts come first; prose and the chart are generated from
those facts deterministically. Every numerical claim links to the validated
information set. Confidence is shown as evidence categories, not as a single
model-confidence number.
"""

from __future__ import annotations

from typing import Any


def _rate(c: int, m: int) -> float | None:
    return None if not m else c / m


def answer_facts(rows: list[dict[str, Any]], dims: list[str], *, origin: str, information_set_id: str | None, cut: dict[str, Any],
                 intent: dict[str, Any], recipe: dict[str, Any]) -> dict[str, Any]:
    totals = {k: sum(int(r[k]) for r in rows) for k in ("eligible_all", "eligible_mature", "immature", "quality_excluded", "converted")}
    by_dim = []
    for r in rows:
        ea = int(r["eligible_all"])
        by_dim.append({**{d: r[d] for d in dims}, "eligible_all": ea, "eligible_mature": int(r["eligible_mature"]),
                       "immature": int(r["immature"]), "quality_excluded": int(r["quality_excluded"]), "converted": int(r["converted"]),
                       "conversion_rate": _rate(int(r["converted"]), int(r["eligible_mature"])),
                       "immature_rate": _rate(int(r["immature"]), ea), "quality_excluded_rate": _rate(int(r["quality_excluded"]), ea)})
    imm_rates = [x["immature_rate"] for x in by_dim if x["immature_rate"] is not None]
    return {
        "metric": recipe["id"], "recipe_version": recipe["version"], "population": intent.get("population"),
        "totals": {**totals, "conversion_rate": _rate(totals["converted"], totals["eligible_mature"]),
                   "immature_rate": _rate(totals["immature"], totals["eligible_all"])},
        "by_dimension": by_dim, "dimensions": dims, "origin": origin, "information_set_id": information_set_id,
        "source_cut": {"cut_mode": cut.get("cut_mode"), "publications": [e["publication_id"] for e in cut.get("entries", [])],
                       "replayable_until": cut.get("replayable_until")},
        "knowledge_cutoff": intent.get("knowledge_cutoff"), "window": intent.get("window"), "cohort": intent.get("cohort"),
        "follow_up_imbalance": (max(imm_rates) - min(imm_rates)) if len(imm_rates) > 1 else None,
    }


def chart_spec(facts: dict[str, Any]) -> dict[str, Any]:
    """Deterministic Vega-Lite spec: bar of mature-opportunity conversion rates."""
    dims = facts["dimensions"]
    data = [{**{d: r[d] for d in dims}, "conversion_rate": r["conversion_rate"], "eligible_mature": r["eligible_mature"],
             "converted": r["converted"]} for r in facts["by_dimension"] if r["conversion_rate"] is not None]
    if not dims:
        x = {"field": "label", "type": "nominal", "title": None}
        for r in data:
            r["label"] = "All"
    else:
        x = {"field": dims[0], "type": "nominal", "title": dims[0].replace("_", " ")}
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": "Conversion among mature opportunities",
        "data": {"values": data},
        "mark": {"type": "bar"},
        "encoding": {
            "x": x,
            "y": {"field": "conversion_rate", "type": "quantitative", "axis": {"format": ".0%"}, "title": "Conversion rate (mature)"},
            "tooltip": [{"field": d, "type": "nominal"} for d in dims] + [
                {"field": "converted", "type": "quantitative"}, {"field": "eligible_mature", "type": "quantitative"},
                {"field": "conversion_rate", "type": "quantitative", "format": ".1%"}],
        },
    }
    if len(dims) > 1:
        spec["encoding"]["xOffset"] = {"field": dims[1]}
        spec["encoding"]["color"] = {"field": dims[1], "type": "nominal", "title": dims[1].replace("_", " ")}
    return spec


def confidence_categories(*, confirmed: bool, recipe_status: str, cut_mode: str, temporal_ok: bool, structural_ok: bool,
                          business_verified: bool = False) -> list[dict[str, Any]]:
    return [
        {"category": "Meaning confirmed", "status": "yes" if confirmed else "no"},
        {"category": "Authoritative recipe resolved", "status": recipe_status},
        {"category": "Source coverage", "status": "complete (pinned publications)" if cut_mode == "pinned" else "provisional"},
        {"category": "Temporal checks", "status": "passed" if temporal_ok else "failed"},
        {"category": "Structural checks", "status": "passed" if structural_ok else "failed"},
        {"category": "Business truth independently verified", "status": "verified" if business_verified else "unverified"},
    ]


def narrative(facts: dict[str, Any], recipe: dict[str, Any]) -> str:
    t = facts["totals"]
    pct = (lambda v: "n/a" if v is None else f"{v:.1%}")
    lines = [
        f"Among {t['eligible_mature']:,} mature opportunities, {t['converted']:,} converted ({pct(t['conversion_rate'])}).",
        f"Of {t['eligible_all']:,} eligible opportunities in the cohort, {t['immature']:,} ({pct(t['immature_rate'])}) were excluded as immature "
        f"(their {facts['window']['value'] if facts.get('window') else ''}-day window ends after the knowledge cutoff {facts['knowledge_cutoff']}) "
        f"and {t['quality_excluded']:,} were quality-excluded.",
    ]
    for r in facts["by_dimension"]:
        label = " / ".join(str(r[d]) for d in facts["dimensions"]) or "All"
        lines.append(f"- {label}: {pct(r['conversion_rate'])} ({r['converted']:,}/{r['eligible_mature']:,} mature); "
                     f"immature {pct(r['immature_rate'])}, quality-excluded {pct(r['quality_excluded_rate'])}.")
    caveats = caveat_list(facts, recipe)
    return "\n".join(lines + ["", "Caveats:"] + [f"- {c}" for c in caveats])


def caveat_list(facts: dict[str, Any], recipe: dict[str, Any]) -> list[str]:
    defs = recipe["definitions"]
    c = [
        "Rates are conversion among mature opportunities only; immature opportunities are excluded from the denominator.",
        "Differences between groups are observed associations; neither mature-only rates nor a survival curve establishes causality.",
    ]
    if recipe["algebra"]["identity"] == "submission":
        c.append("A submission is counted once; dimensions come from its first eligible quoted branch "
                 f"({defs['attribution_rule']['tie_break_rule_id']}), and a bind on any linked branch converts it.")
    else:
        c.append("Legacy definition: each quote branch is counted independently using UTC dates and an inclusive day window.")
    c.append(f"Cancellation treatment: {defs['cancellation_treatment'].replace('_', ' ')}.")
    if facts.get("follow_up_imbalance") is not None and facts["follow_up_imbalance"] > 0.15:
        c.append(f"Follow-up imbalance: immature rates differ by {facts['follow_up_imbalance']:.0%} across groups (e.g. rollout timing); "
                 "comparisons may be biased. Consider a later cutoff, matched observation cohorts, or a separately approved survival recipe.")
    if facts["source_cut"]["cut_mode"] != "pinned":
        c.append("Provisional: source publications do not yet cover the cutoff.")
    return c
