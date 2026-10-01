"""Learned selection among certified physical variants (section 8, Bao-inspired).

Actions are certified fused/staged/partitioned variants already on the Pareto
frontier — never hints inside the engine. Selection starts rule-based; a
workload-scoped epsilon-greedy policy takes over only after every frontier arm
has enough comparable receipts in the same workload signature. Exploration is
bounded (probability and estimated-cost ratio) and every decision records its
features, model version, selection probability and baseline.
"""

from __future__ import annotations

import math
import random
import statistics
from typing import Any

from .canonical import fingerprint, short

SELECTOR_VERSION = "eps-greedy/1"
DDL = """CREATE TABLE IF NOT EXISTS selector_observations (signature TEXT, variant TEXT, latency_s REAL, credits REAL,
  execution_id TEXT PRIMARY KEY, at TEXT)"""


def signature(recipe_id: str, dims: list[str], stats: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    feats = {"recipe": recipe_id, "dims": sorted(dims),
             "cohort_bucket": int(math.log2(max(1, stats.get("cohort_rows") or 1))),
             "fanout_bucket": int(math.log2(max(1, stats.get("bind_multiplicity_max") or 1)))}
    return "wl_" + short(fingerprint(feats), 12), feats


class VariantSelector:
    def __init__(self, store, min_samples: int = 3, epsilon: float = 0.1, max_cost_ratio: float = 2.0):
        self.store = store
        self.min_samples = min_samples
        self.epsilon = epsilon
        self.max_cost_ratio = max_cost_ratio
        store._con().execute(DDL)

    def observe(self, sig: str, variant: str, latency_s: float, credits: float | None, execution_id: str, at: str) -> None:
        self.store.x("INSERT OR REPLACE INTO selector_observations VALUES (?,?,?,?,?,?)", (sig, variant, latency_s, credits, execution_id, at))

    def stats(self, sig: str) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for r in self.store.q("SELECT variant, latency_s FROM selector_observations WHERE signature=?", (sig,)):
            out.setdefault(r["variant"].split("+")[0], []).append(r["latency_s"])
        return out

    def select(self, sig: str, feats: dict[str, Any], frontier: list[dict[str, Any]], baseline: str, seed: str) -> dict[str, Any]:
        """frontier: [{variant, latency_p90_s, credits_high}]; baseline: rule-based choice."""
        obs = self.stats(sig)
        arms = [f["variant"] for f in frontier]
        decision = {"selector_version": SELECTOR_VERSION, "signature": sig, "features": feats, "baseline": baseline,
                    "arms": {a: {"n": len(obs.get(a, [])), "mean_latency_s": round(statistics.mean(obs[a]), 5) if obs.get(a) else None}
                             for a in arms}}
        if len(arms) < 2 or any(len(obs.get(a, [])) < self.min_samples for a in arms):
            return {**decision, "mode": "rule_based", "chosen": baseline, "selection_probability": 1.0,
                    "reason": f"insufficient comparable receipts (need {self.min_samples} per frontier arm)"}
        best = min(arms, key=lambda a: statistics.mean(obs[a]))
        base_cost = next(f["credits_high"] for f in frontier if f["variant"] == best)
        explorable = [f["variant"] for f in frontier if f["variant"] != best and f["credits_high"] <= base_cost * self.max_cost_ratio]
        rng = random.Random(seed)
        if explorable and rng.random() < self.epsilon:
            chosen = rng.choice(explorable)
            p = self.epsilon / len(explorable)
            mode = "explore"
        else:
            chosen = best
            p = 1 - self.epsilon if explorable else 1.0
            mode = "exploit"
        return {**decision, "mode": mode, "chosen": chosen, "selection_probability": round(p, 4),
                "reason": f"best measured mean latency: {best}" if mode == "exploit" else "bounded exploration within cost ratio"}
