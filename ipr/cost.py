"""Estimation, Pareto selection and budget admission (sections 8, 8.1).

Credits here are *simulated credit-equivalents* for the documented Gen1
warehouse rates (XS=1, S=2, M=4, L=8 credits/hour) and the 60-second resume
minimum. Timeout x rate is an admission proxy, not an invoice ceiling; the
local SQLite engine incurs no real credits. Mandatory correctness and security
are constraints, never weighted terms.
"""

from __future__ import annotations

import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class PlanEstimate:
    variant: str
    latency_p50_s: float
    latency_p90_s: float
    credits_low: float
    credits_high: float
    resume_floor_credits: float
    stage_writes: int
    node_count: int
    uncertainty: float
    critical_path: list[str]
    feasible: bool = True
    infeasible_reasons: list[str] = field(default_factory=list)
    objective: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Calibration:
    """Observed/estimated duration ratios per operator, from receipts."""

    def __init__(self):
        self._ratios: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def observe(self, operator: str, estimated_s: float, actual_s: float) -> None:
        if estimated_s > 0 and actual_s >= 0:
            with self._lock:
                self._ratios.setdefault(operator, []).append(actual_s / estimated_s)
                self._ratios[operator] = self._ratios[operator][-50:]

    def factor(self, operator: str) -> float:
        with self._lock:
            r = self._ratios.get(operator)
            return statistics.median(r) if r and len(r) >= 3 else 1.0


class CostModel:
    BASE_OVERHEAD_S = {"sql_ctas": 0.04, "validate": 0.02, "publish": 0.01, "present": 0.005}
    PER_ROW_S = {"canonical_event_selection": 6e-6, "key_reduction": 5e-6, "window_exists": 8e-6,
                 "aggregate_sufficient_stats": 1.2e-5, "union_disjoint_partitions": 1e-5}

    def __init__(self, policy: dict[str, Any], calibration: Calibration | None = None):
        self.policy = policy
        self.calibration = calibration or Calibration()

    def node_seconds(self, node: dict[str, Any], stats: dict[str, Any]) -> float:
        base = self.BASE_OVERHEAD_S.get(node["kind"], 0.02)
        if node["kind"] != "sql_ctas":
            return base
        cohort = float(stats.get("cohort_rows", 1000))
        binds = float(stats.get("linked_bind_rows", cohort * 0.4))
        op = node["operator"]
        if op == "canonical_event_selection":
            rows = cohort * float(stats.get("branches_per_opportunity", 1.6))
        elif op == "key_reduction":
            rows = binds * 2 + cohort
        elif op == "window_exists":
            rows = (cohort + binds)
            if node.get("partition") is not None:
                rows = rows / max(1, int(node["params"].get("n_partitions", 1))) + cohort * 0.15
        elif op == "aggregate_sufficient_stats" and len(node.get("logical_nodes", [])) > 1:
            rows = cohort * 2.2 + binds * 3  # fused statement
        else:
            rows = float(node["envelope"]["estimate"] if node.get("envelope") else 10)
        heavy = float(stats.get("bind_multiplicity_max", 1))
        skew_penalty = 1.0 + min(2.0, heavy / 200.0)
        return (base + rows * self.PER_ROW_S.get(op, 5e-6) * skew_penalty) * self.calibration.factor(op)

    def estimate(self, phys: dict[str, Any], stats: dict[str, Any], workers: int, warm: bool,
                 warehouse_class: str | None = None) -> PlanEstimate:
        durations = {n["id"]: self.node_seconds(n, stats) for n in phys["nodes"]}
        finish: dict[str, float] = {}
        prev: dict[str, str | None] = {}
        for nid in phys["topological_order"]:
            n = next(x for x in phys["nodes"] if x["id"] == nid)
            start = max((finish[d] for d in n["depends_on"]), default=0.0)
            prev[nid] = max(n["depends_on"], key=lambda d: finish[d]) if n["depends_on"] else None
            finish[nid] = start + durations[nid]
        end = max(finish, key=finish.get)
        path = []
        cur: str | None = end
        while cur:
            path.append(cur)
            cur = prev[cur]
        critical = finish[end]
        total_work = sum(durations.values())
        p50 = max(critical, total_work / max(1, workers))
        uncertainty = float(stats.get("uncertainty", 3.0))
        p90 = p50 * (1 + uncertainty / 2)
        wclass = warehouse_class or self.policy["default_class"]
        rate = self.policy["warehouse_rates_credits_per_hour"][wclass] / 3600.0
        resume = 0.0 if warm else self.policy["resume_minimum_seconds"] * rate
        work = total_work * rate
        low = max(resume, work / uncertainty) + self.policy["service_overhead_credits"]
        high = max(resume, work * uncertainty * (1 + self.policy["retry_allowance_fraction"])) + self.policy["service_overhead_credits"]
        writes = sum(1 for n in phys["nodes"] if n.get("materialization") == "stage")
        return PlanEstimate(variant=phys["variant"], latency_p50_s=round(p50, 4), latency_p90_s=round(p90, 4),
                            credits_low=round(low, 6), credits_high=round(high, 6), resume_floor_credits=round(resume, 6),
                            stage_writes=writes, node_count=len(phys["nodes"]), uncertainty=uncertainty,
                            critical_path=list(reversed(path)))


def pareto_frontier(estimates: list[PlanEstimate]) -> list[PlanEstimate]:
    front = []
    for e in estimates:
        dominated = any(
            (o.latency_p90_s <= e.latency_p90_s and o.credits_high <= e.credits_high and o.stage_writes <= e.stage_writes)
            and (o.latency_p90_s < e.latency_p90_s or o.credits_high < e.credits_high or o.stage_writes < e.stage_writes)
            for o in estimates if o is not e)
        if not dominated:
            front.append(e)
    return front


def choose(estimates: list[PlanEstimate], target_credits: float, max_runtime_s: float,
           weights: dict[str, float] | None = None) -> tuple[PlanEstimate | None, list[PlanEstimate]]:
    w = weights or {"cost": 1.0, "latency": 1.0, "uncertainty": 0.1, "materialization": 0.02}
    for e in estimates:
        e.infeasible_reasons = []
        if e.credits_high > target_credits:
            e.infeasible_reasons.append(f"credits_high {e.credits_high} > target {target_credits}")
        if e.latency_p90_s > max_runtime_s:
            e.infeasible_reasons.append(f"latency_p90 {e.latency_p90_s}s > {max_runtime_s}s")
        e.feasible = not e.infeasible_reasons
        e.objective = round(w["cost"] * e.credits_high / max(target_credits, 1e-9) + w["latency"] * e.latency_p90_s / max_runtime_s
                            + w["uncertainty"] * e.uncertainty / 10 + w["materialization"] * e.stage_writes, 6)
    front = pareto_frontier([e for e in estimates if e.feasible])
    if not front:
        return None, []
    return min(front, key=lambda e: (e.objective, e.node_count)), front


class WarehousePool:
    """Simulated warm-pool state for admission arithmetic (resume floor)."""

    def __init__(self, auto_suspend_s: float = 60):
        self.auto_suspend_s = auto_suspend_s
        self._resumed_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_warm(self, wclass: str) -> bool:
        with self._lock:
            return self._resumed_until.get(wclass, 0) > time.monotonic()

    def touch(self, wclass: str) -> None:
        with self._lock:
            self._resumed_until[wclass] = time.monotonic() + self.auto_suspend_s


class BudgetBroker:
    def __init__(self, policy: dict[str, Any], pool: WarehousePool):
        self.policy = policy
        self.pool = pool
        self._reserved: dict[str, float] = {}
        self._lock = threading.Lock()

    def resume_floor(self, wclass: str) -> float:
        return self.policy["warehouse_rates_credits_per_hour"][wclass] * self.policy["resume_minimum_seconds"] / 3600.0

    def admit_probe(self, target_credits: float, est_seconds: float, wclass: str | None = None) -> dict[str, Any]:
        wclass = wclass or self.policy["default_class"]
        warm = self.pool.is_warm(wclass)
        rate = self.policy["warehouse_rates_credits_per_hour"][wclass] / 3600.0
        need = max(0.0 if warm else self.resume_floor(wclass), est_seconds * rate) + self.policy["service_overhead_credits"]
        decision = {"warehouse_class": wclass, "warm": warm, "required_credits": round(need, 6), "target": target_credits,
                    "resume_floor": round(self.resume_floor(wclass), 6)}
        if need > target_credits:
            smaller = [c for c in self.policy["allowed_classes"]
                       if self.resume_floor(c) + self.policy["service_overhead_credits"] <= target_credits]
            decision.update(admitted=False, reason=f"allocation {target_credits} cannot cover {'warm run' if warm else 'cold resume floor'} "
                            f"{need:.4f} on {wclass}", alternatives=smaller)
        else:
            decision.update(admitted=True, reason="within allocation")
        return decision

    def reserve(self, key: str, credits: float, limit: float) -> bool:
        with self._lock:
            total = self._reserved.get(key, 0.0) + credits
            if total > limit + 1e-12:
                return False
            self._reserved[key] = total
            return True

    def reserved(self, key: str) -> float:
        with self._lock:
            return self._reserved.get(key, 0.0)
