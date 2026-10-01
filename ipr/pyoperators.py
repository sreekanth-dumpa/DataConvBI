"""Approved Python operators (section 7).

The agent selects an operator id and bounded parameters; it never sends code.
Each operator is pinned by a digest of its source plus the interpreter version
and declares its determinism class: deterministic float operators carry a
tolerance, not bit-for-bit repeatability guarantees across platforms.
"""

from __future__ import annotations

import inspect
import math
import platform
from statistics import NormalDist
from typing import Any, Callable

from .canonical import sha256_hex


def wilson_interval(rows: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    """95% (configurable) Wilson score interval for converted / eligible_mature per output row."""
    level = float(params.get("confidence_level", 0.95))
    z = NormalDist().inv_cdf(0.5 + level / 2)
    out = []
    for r in rows:
        n, k = int(r["eligible_mature"]), int(r["converted"])
        if n == 0:
            out.append({**r, "ci_low": None, "ci_high": None, "ci_level": level})
            continue
        p = k / n
        denom = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
        out.append({**r, "ci_low": max(0.0, centre - half), "ci_high": min(1.0, centre + half), "ci_level": level})
    return out


def _spec(fn: Callable, version: str, determinism: str, params: dict[str, Any], tolerance: float | None) -> dict[str, Any]:
    return {"fn": fn, "version": version, "determinism": determinism, "params": params, "tolerance": tolerance,
            "code_digest": "sha256:" + sha256_hex(inspect.getsource(fn)), "runtime": f"python-{platform.python_version()}",
            "dependency_lock": "stdlib-only", "accepts_grain": "output_dimensions", "adds_columns": ["ci_low", "ci_high", "ci_level"]}


PY_OPERATORS: dict[str, dict[str, Any]] = {
    "wilson_interval": _spec(wilson_interval, "1.0.0", "deterministic_float_tolerance",
                             {"confidence_level": {"type": "number", "min": 0.8, "max": 0.99, "default": 0.95}}, 1e-12),
}


def validate_params(op_id: str, params: dict[str, Any]) -> dict[str, Any]:
    spec = PY_OPERATORS[op_id]
    clean = {}
    for name, rule in spec["params"].items():
        v = params.get(name, rule["default"])
        if not isinstance(v, (int, float)) or not rule["min"] <= float(v) <= rule["max"]:
            raise ValueError(f"{op_id}.{name}={v!r} outside [{rule['min']}, {rule['max']}]")
        clean[name] = float(v)
    return clean


def descriptor(op_id: str) -> dict[str, Any]:
    s = PY_OPERATORS[op_id]
    return {k: v for k, v in s.items() if k != "fn"} | {"operator_id": op_id}


def verify_digest(op_id: str, digest: str) -> bool:
    return PY_OPERATORS[op_id]["code_digest"] == digest
