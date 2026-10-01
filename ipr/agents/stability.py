"""Proposal stability under concurrency (section 21.1, inference stability).

Repeats a skill on fixed inputs (optionally concurrently) and reports agreement
of canonical outputs, separately from fixed-IR execution replay. Rationale
text is excluded from the comparison: consistent information does not require
identical wording.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..canonical import fingerprint


def _canonical(output: dict[str, Any], ignore: tuple[str, ...]) -> str:
    return fingerprint({k: v for k, v in output.items() if k not in ignore})


def evaluate_stability(runner, skill: str, inputs: dict[str, Any], repeats: int = 5, concurrency: int = 1,
                       ignore: tuple[str, ...] = ("rationale", "narrative", "headline", "confirmation_summary"), guard=None) -> dict[str, Any]:
    def one(_):
        r = runner.invoke(skill, inputs, guard=guard)
        return _canonical(r.output, ignore), r.provider, r.fallback_used

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        results = list(pool.map(one, range(repeats)))
    counts = Counter(r[0] for r in results)
    modal = counts.most_common(1)[0][1]
    return {"skill": skill, "repeats": repeats, "concurrency": concurrency, "distinct_outputs": len(counts),
            "agreement": round(modal / repeats, 4), "numerator": modal, "denominator": repeats,
            "providers": sorted({r[1] for r in results}), "fallbacks": sum(1 for r in results if r[2]),
            "note": "proposal stability only; fixed-IR replay equivalence is measured separately"}
