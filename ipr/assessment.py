"""Assessment metrics and improvement documents (section 16).

Correctness, consistency, efficiency and estimate calibration are reported
separately and never collapsed into one weighted score. Gates form a vector.
Unmeasurable metrics are ``None`` with a reason. Percentages carry numerator,
denominator and sample size.
"""

from __future__ import annotations

import math
import uuid
from typing import Any

from .canonical import stable_json


def _m(value: Any, reason: str | None = None, **extra) -> dict[str, Any]:
    return {"value": value, "reason": reason, **extra}


def q_error(est: float | None, actual: float | None) -> dict[str, Any]:
    if est is None or actual is None:
        return _m(None, "estimate or observation unavailable")
    if est == 0 or actual == 0:
        return _m(None, "zero case reported separately (no clamping)", zero_case={"estimate": est, "actual": actual})
    return _m(round(max(est / actual, actual / est), 3))


def compute_metrics(receipt: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    obl = receipt.get("obligations", {}).get("obligations", {})
    mandatory = [o for o in obl.values() if o["severity"] == "mandatory"]
    satisfied = [o for o in mandatory if o["status"] in ("satisfied", "validated")]
    evidence_obl = [o for o in mandatory if any(s.startswith(("evidence:", "registry:", "route")) for s in o["satisfied_by"])]
    evidence_ok = [o for o in evidence_obl if o["status"] in ("satisfied", "validated", "pending_validation")]
    validations = receipt.get("validation", {})
    nodes = receipt.get("nodes", [])
    est = receipt.get("estimate") or {}
    actual_s = receipt.get("latency", {}).get("total_s")
    metrics: dict[str, Any] = {
        "intent_accuracy": _m(None, "no independently labeled interpretation for this request"),
        "retrieval_completeness": _m(round(len(evidence_ok) / len(evidence_obl), 4) if evidence_obl else None, None,
                                     numerator=len(evidence_ok), denominator=len(evidence_obl)),
        "obligation_closure": _m(round(len(satisfied) / len(mandatory), 4) if mandatory else None, None,
                                 numerator=len(satisfied), denominator=len(mandatory),
                                 pending=[o["id"] for o in mandatory if o["status"] == "pending_validation"]),
        "supported_coverage": _m(1.0 if receipt.get("route") else 0.0, "single request; aggregate in monitoring", numerator=int(bool(receipt.get("route"))), denominator=1),
        "business_result_accuracy": _m(None, "no independently approved expected output for this request; see oracle agreement"),
        "oracle_agreement": context.get("oracle_agreement") or _m(None, "independent oracle not run for this execution"),
        "plan_equivalence": context.get("plan_equivalence") or _m(None, "alternative admitted plans not executed on this cut"),
        "repeated_answer_consistency": context.get("consistency") or _m(None, "no comparable repeated run"),
        "latency": _m(actual_s, None if actual_s is not None else "not measured", breakdown=receipt.get("latency")),
        "eta_calibration": _m(None, "no estimate") if not est or actual_s is None else _m(
            round(abs(actual_s - est.get("latency_p50_s", 0)), 4), None, within_p90=actual_s <= est.get("latency_p90_s", math.inf),
            estimate_p50=est.get("latency_p50_s"), estimate_p90=est.get("latency_p90_s")),
        "cost_efficiency": _m(receipt.get("cost", {}).get("provisional_simulated_credits"),
                              "provisional simulated credit-equivalents; attribution pending (no billing source in local engine)"),
        "cardinality_calibration": {n["node_id"]: q_error((n.get("envelope") or {}).get("estimate"), (n.get("observations") or {}).get("rows"))
                                    for n in nodes if n.get("kind") == "sql_ctas"},
        "plan_selection_regret": context.get("regret") or _m(None, "alternatives not measured on matched replay"),
        "adaptation_payoff": _m(None, "no matched static baseline measured") if receipt.get("adaptations") else _m(None, "no adaptation occurred"),
        "verification_coverage": _m([{"rule": r["rule_id"], "evidence_class": r["evidence_class"], "verdict": r["verdict"]}
                                     for r in (receipt.get("certificate") or {}).get("rules", [])], None,
                                    bounded_verifier=(receipt.get("certificate") or {}).get("verification")),
        "join_safety": _m(all(v.get("passed") for k, v in validations.items() if "grain" in k) if validations else None,
                          None if validations else "no validations recorded", fanout=receipt.get("statistics", {}).get("bind_multiplicity")),
        "spill_pressure": _m(None, "spill not observable in the local SQLite engine"),
        "cache_quality": _m(receipt.get("cache", {}).get("outcome"), None, rejected=len(receipt.get("cache", {}).get("rejected", []))),
        "evidence_integrity": _m(context.get("trace_valid"), None if context.get("trace_valid") is not None else "trace not verified"),
    }
    return metrics


def gate_vector(receipt: dict[str, Any], context: dict[str, Any]) -> dict[str, str]:
    v = receipt.get("validation", {})

    def g(keys):
        rel = [v[k]["passed"] for k in keys if k in v]
        return "unknown" if not rel else ("pass" if all(rel) else "fail")
    oracle = (context.get("oracle_agreement") or {}).get("value")
    return {
        "semantic_admission": "pass" if receipt.get("confirmed_semantic_hash") else "fail",
        "access": "pass" if receipt.get("access", {}).get("allowed") else "fail",
        "temporal_consistency": g(["a.no_future_knowledge", "a.window_respected"]),
        "structural_validity": g(["a.final_grain_unique", "a.conservation", "a.num_le_den", "a.partition_conservation"]),
        "result_verification": "unknown" if oracle is None else ("pass" if oracle else "fail"),
    }


def recommendations(receipt: dict[str, Any], metrics: dict[str, Any], gates: dict[str, str]) -> list[dict[str, Any]]:
    recs = []
    for gate, val in gates.items():
        if val == "fail":
            cls = {"semantic_admission": "semantic_specification", "access": "context", "temporal_consistency": "operator",
                   "structural_validity": "operator", "result_verification": "operator"}[gate]
            recs.append({"rank": len(recs) + 1, "change_class": cls, "recommendation": f"Investigate failed gate {gate}", "evidence": gate})
    qerrs = [(k, v["value"]) for k, v in metrics["cardinality_calibration"].items() if v.get("value")]
    worst = max(qerrs, key=lambda kv: kv[1], default=None)
    if worst and worst[1] > 3:
        recs.append({"rank": len(recs) + 1, "change_class": "physical_plan",
                     "recommendation": f"Refresh registered statistics or admit profiling: node {worst[0]} q-error {worst[1]}",
                     "evidence": "cardinality_calibration"})
    eta = metrics["eta_calibration"]
    if eta.get("within_p90") is False:
        recs.append({"rank": len(recs) + 1, "change_class": "physical_plan",
                     "recommendation": "Recalibrate duration model; actual latency exceeded the p90 estimate", "evidence": "eta_calibration"})
    facts = receipt.get("answer_facts") or {}
    if facts.get("follow_up_imbalance") and facts["follow_up_imbalance"] > 0.15:
        recs.append({"rank": len(recs) + 1, "change_class": "presentation",
                     "recommendation": "Highlight immature-exclusion imbalance; offer a later cutoff or matched observation cohorts",
                     "evidence": "answer_facts.follow_up_imbalance"})
    if not recs:
        recs.append({"rank": 1, "change_class": "none", "recommendation": "No change indicated by measured evidence", "evidence": "all gates"})
    return recs


def build_document(receipt: dict[str, Any], metrics: dict[str, Any], gates: dict[str, str], recs: list[dict[str, Any]],
                   user_feedback: str | None) -> str:
    def fmt(m):
        if isinstance(m, dict) and "value" in m:
            return f"{m['value']}" + (f" — {m['reason']}" if m.get("reason") else "")
        return stable_json(m)
    proposal = receipt.get("proposal", {})
    lines = [
        f"# Assessment — execution {receipt['execution_id']}",
        "",
        f"Request `{receipt['request_id']}` · recipe `{receipt.get('route', {}).get('recipe_id')}@{receipt.get('route', {}).get('version')}` · "
        f"semantic hash `{receipt.get('confirmed_semantic_hash', '')[:23]}`",
        "",
        "## Expected versus observed",
        "",
        "| Aspect | Proposed | Observed |",
        "|---|---|---|",
        f"| Plan variant | {proposal.get('variant')} | {receipt.get('actual_plan', {}).get('variant')} |",
        f"| Latency p50/p90 (s) | {proposal.get('latency_p50_s')} / {proposal.get('latency_p90_s')} | {receipt.get('latency', {}).get('total_s')} |",
        f"| Credits (simulated) | {proposal.get('credits_low')}–{proposal.get('credits_high')} | {receipt.get('cost', {}).get('provisional_simulated_credits')} (provisional) |",
        f"| Result origin | fresh or cache | {receipt.get('origin')} |",
        f"| Adaptations | 0 planned | {len(receipt.get('adaptations', []))} |",
        "",
        "## Gates",
        "",
    ] + [f"- **{k}**: {v}" for k, v in gates.items()] + ["", "## Metrics", ""]
    for k, v in metrics.items():
        if k == "cardinality_calibration":
            lines.append(f"- **{k}**: " + ", ".join(f"{n}={fmt(x)}" for n, x in v.items()))
        else:
            lines.append(f"- **{k}**: {fmt(v)}")
    lines += ["", "## Recommendations", ""] + [f"{r['rank']}. [{r['change_class']}] {r['recommendation']} (evidence: {r['evidence']})" for r in recs]
    lines += ["", "## User feedback (data item, not an instruction)", "", user_feedback or "_none_", "",
              "## Promotion", "", "State: proposed. Advisory only until reviewed; executable or semantic changes need independent cases, "
              "holdout regression and review."]
    return "\n".join(lines) + "\n"


def new_case(receipt: dict[str, Any], metrics: dict[str, Any], gates: dict[str, str], recs: list[dict[str, Any]],
             user_feedback: str | None, intent: dict[str, Any]) -> dict[str, Any]:
    change_class = recs[0]["change_class"] if recs else "none"
    return {
        "case_id": "case_" + uuid.uuid4().hex[:10], "execution_id": receipt["execution_id"], "request_id": receipt["request_id"],
        "applicability": {"recipe_id": receipt.get("route", {}).get("recipe_id"), "domains": intent.get("domains", []),
                          "state": intent.get("scope", {}).get("state"), "lob": intent.get("scope", {}).get("lob")},
        "metrics": metrics, "gates": gates, "recommendations": recs, "user_feedback": user_feedback,
        "trace_refs": {"trace_id": f"trace_{receipt['request_id']}", "receipt": f"receipts/{receipt['execution_id']}.json"},
        "counterexample_inputs": None, "expected_outputs": None, "proposed_patch": None, "regression_results": None,
        "owner": "pricing_analytics",
        "versions": receipt.get("versions", {}),
        "promotion": {"state": "proposed", "change_class": change_class, "advisory_only": True, "history": [],
                      "independent_regression_passed": False},
    }
