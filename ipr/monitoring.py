"""Reference monitoring (section 20).

Request-, node- and cost-level metrics from the control store, availability
states for telemetry, and workload-calibrated alert policies. Estimated,
observed, approximate, pending and unavailable values stay distinct.
"""

from __future__ import annotations

from typing import Any

from .store import Store, loads

DEFAULT_ALERTS = {
    "queue_age_s": 30.0,
    "failed_execution_rate": 0.2,
    "blocked_grain_violations": 0,
    "missing_receipts": 0,
    "abandoned_stages": 0,
}


def _pct(n: int, d: int) -> dict[str, Any]:
    return {"value": None if d == 0 else round(n / d, 4), "numerator": n, "denominator": d}


def snapshot(store: Store, clock, alert_policy: dict[str, float] | None = None) -> dict[str, Any]:
    policy = {**DEFAULT_ALERTS, **(alert_policy or {})}
    res = [loads(r["resolution_json"]) for r in store.q(
        "SELECT r.resolution_json FROM resolutions r JOIN (SELECT request_id, MAX(version) v FROM resolutions GROUP BY request_id) m "
        "ON m.request_id=r.request_id AND m.v=r.version")]
    n_req = len(res)
    clar = sum(1 for r in res if r["status"] == "clarification_required")
    unsup = sum(1 for r in res if r["status"] in ("unsupported", "missing_definition"))
    ex = store.q("SELECT state, COUNT(*) c FROM executions GROUP BY state")
    by_state = {r["state"]: r["c"] for r in ex}
    total_ex = sum(by_state.values())
    queue = store.q("SELECT COUNT(*) c FROM nodes WHERE state='ready'")[0]["c"]
    cache_events = store.q("SELECT event, COUNT(*) c FROM cache_events GROUP BY event")
    ce = {r["event"]: r["c"] for r in cache_events}
    hits = ce.get("hit_exact", 0) + ce.get("hit_derived", 0)
    lookups = store.q("SELECT COUNT(*) c FROM executions")[0]["c"] + hits
    tele = store.q("SELECT metric, state, COUNT(*) c FROM telemetry GROUP BY metric, state")
    attempts = store.q("SELECT state, COUNT(*) c FROM node_attempts GROUP BY state")
    grain_viol = store.q("SELECT COUNT(*) c FROM node_attempts WHERE state='validation_failed'")[0]["c"]
    succeeded = store.q("SELECT execution_id FROM executions WHERE state='succeeded'")
    missing_receipts = sum(1 for e in succeeded if not store.one("SELECT 1 AS x FROM receipts WHERE execution_id=?", (e["execution_id"],)))
    now = clock.now_z()
    abandoned = store.q("SELECT COUNT(*) c FROM stages WHERE expires_at < ? AND state IN ('complete','pending')", (now,))[0]["c"]
    node_rows = store.q("SELECT a.node_id, a.metrics_json FROM node_attempts a WHERE a.state='complete' ORDER BY a.submitted_at DESC LIMIT 200")
    qerr = []
    for r in node_rows:
        m = loads(r["metrics_json"]) or {}
        env = m.get("envelope") or {}
        if env.get("observed") and env.get("expected") and env["expected"][1]:
            est = (env["expected"][0] + env["expected"][1]) / 2
            if est > 0:
                qerr.append(max(est / env["observed"], env["observed"] / est))
    try:
        agent_rows = store.q("SELECT skill, provider, COUNT(*) c, SUM(fallback_used) f, AVG(latency_ms) l FROM agent_invocations GROUP BY skill, provider")
    except Exception:  # noqa: BLE001 - table created lazily by the agent runner
        agent_rows = []
    metrics = {
        "agent_invocations": [{"skill": r["skill"], "provider": r["provider"], "invocations": r["c"], "fallbacks": r["f"],
                               "mean_latency_ms": round(r["l"] or 0, 3)} for r in agent_rows],
        "requests": n_req,
        "clarification_rate": _pct(clar, n_req),
        "unsupported_route_rate": _pct(unsup, n_req),
        "executions_by_state": by_state,
        "failed_execution_rate": _pct(by_state.get("failed", 0), total_ex),
        "queue_depth_ready_nodes": queue,
        "validated_cache_hit_rate": _pct(hits, lookups),
        "cache_events": ce,
        "attempt_states": {r["state"]: r["c"] for r in attempts},
        "telemetry_availability": [dict(r) for r in tele],
        "blocked_grain_violations": grain_viol,
        "missing_receipts": missing_receipts,
        "abandoned_stages": abandoned,
        "median_cardinality_q_error": sorted(qerr)[len(qerr) // 2] if qerr else None,
    }
    alerts = []
    if metrics["failed_execution_rate"]["value"] and metrics["failed_execution_rate"]["value"] > policy["failed_execution_rate"]:
        alerts.append("failed execution rate above policy")
    if grain_viol > policy["blocked_grain_violations"]:
        alerts.append(f"{grain_viol} blocked grain violations")
    if missing_receipts > policy["missing_receipts"]:
        alerts.append(f"{missing_receipts} succeeded executions without receipts")
    if abandoned > policy["abandoned_stages"]:
        alerts.append(f"{abandoned} expired stage relations awaiting sweep")
    return {"metrics": metrics, "alerts": alerts, "alert_policy": policy,
            "note": "thresholds are workload-calibrated policies, not universal defaults"}
