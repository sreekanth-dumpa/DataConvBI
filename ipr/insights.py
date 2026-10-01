"""Per-statement diagnostic insights (section 20, Query Insights analogue).

Evaluated per SQL statement from completed-statement statistics, envelopes,
dispatch latency and EXPLAIN planning information. An absent insight is
*unknown* evidence, never a passing assertion; each insight type records its
availability. Insights can propose physical changes but never alter filters.
"""

from __future__ import annotations

from typing import Any

THRESHOLDS = {"exploding_join_ratio": 3.0, "queue_ms": 1000.0}


def collect(nodes: list[dict[str, Any]], attempts: list[dict[str, Any]], explain: dict[str, list[str]]) -> list[dict[str, Any]]:
    out = []
    obs = {n["node_id"]: (n.get("observations") or {}) for n in nodes}
    cohort_rows = (obs.get("cohort") or {}).get("rows")
    for a in attempts:
        if a["state"] != "complete":
            continue
        m = a.get("metrics") or {}
        node = a["node_id"]
        env = m.get("envelope") or {}
        items = []
        if node == "linked_binds" and cohort_rows:
            ratio = (m.get("rows") or 0) / max(1, cohort_rows)
            items.append({"type": "exploding_join", "available": True, "triggered": ratio > THRESHOLDS["exploding_join_ratio"],
                          "value": round(ratio, 3), "detail": "linked bind rows per cohort row"})
        else:
            items.append({"type": "exploding_join", "available": False, "reason": "no join input/output pair observable for this statement"})
        if env.get("deviation") is not None:
            items.append({"type": "cardinality_outside_envelope", "available": True, "triggered": env["deviation"] != "within",
                          "value": env})
        plan = explain.get(node)
        if plan:
            scans = [p for p in plan if p.startswith("SCAN") and "INDEX" not in p]
            items.append({"type": "full_scan_predicate", "available": True, "triggered": bool(scans), "value": scans[:3],
                          "detail": "EXPLAIN planning information, not measured cost"})
        else:
            items.append({"type": "full_scan_predicate", "available": False, "reason": "statement not explainable before upstream stages exist"})
        items.append({"type": "spill", "available": False, "reason": "spill not observable in the local engine"})
        q = m.get("dispatch_latency_ms")
        items.append({"type": "queued_overload", "available": q is not None,
                      **({"triggered": q > THRESHOLDS["queue_ms"], "value": q} if q is not None else {"reason": "dispatch latency not recorded"})})
        out.append({"node_id": node, "attempt_id": a["attempt_id"], "query_id": a.get("query_id"), "insights": items})
    return out
