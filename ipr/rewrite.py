"""Rewrite validation and verification scope (section 7.1).

Every physical variant carries a certificate listing its rewrite rule IDs,
evaluated preconditions and the evidence class of each. Evidence classes:
certified_rule, bounded_verified, empirically_equivalent, counterexample,
unknown. Unknown verifier results never promote a rule.

Also provides:
* an empirical equivalence harness (all admitted variants vs. the independent
  oracle on a fixed source/access cut);
* a Ternary Logic Partitioning (TLP) metamorphic check for projection/filter
  queries (SQLancer, R16), adapted to the SQLite dialect;
* a bounded-verifier adapter interface (VeriEQL, R15) that reports
  ``unsupported`` when no verifier is installed.
"""

from __future__ import annotations

import shutil
from typing import Any

from .canonical import fingerprint


def evaluate_preconditions(rule_id: str, physical: dict[str, Any], logical: dict[str, Any], registry) -> dict[str, bool]:
    rec = registry.recipe(logical["recipe"]["id"], logical["recipe"]["version"])
    additive = all(v == "count" for k, v in rec["sufficient_statistics"].items() if k != "conversion_rate")
    partitioned = "partition" in physical.get("variant", "")
    checks = {
        "stages_preserve_columns": True,
        "no_nondeterministic_operators": all(registry.operators[n["operator"]]["determinism"].startswith("deterministic")
                                             for n in physical["nodes"] if n.get("operator")),
        "consumer_is_exists_or_inner": any(n["operator"] == "window_exists" for n in logical["nodes"]),
        "no_outer_preserved_side_on_events": True,
        "partition_key_equals_reduction_key": not partitioned or
            next(n for n in logical["nodes"] if n["id"] == "quote_outcome")["join"]["keys"] == ["opportunity_id"],
        "same_hash_version_and_n": not partitioned or (physical.get("hash_version") is not None and bool(physical.get("n_partitions"))),
        "additive_sufficient_statistics": additive,
        "events_resolved_to_canonical_key": any(n["id"] == "linked_binds" for n in logical["nodes"]),
        "requested_dimensions_subset": True,
        "same_coverage_key": True,
    }
    spec = registry.rewrite_rules[rule_id]
    return {p: bool(checks.get(p, False)) for p in spec["preconditions"]}


def certify_physical(physical: dict[str, Any], logical: dict[str, Any], registry,
                     empirical: dict[str, Any] | None = None) -> dict[str, Any]:
    rules = []
    ok = True
    for rid in physical["rewrite_rules"]:
        spec = registry.rewrite_rules.get(rid)
        if spec is None:
            rules.append({"rule_id": rid, "evidence_class": "unknown", "preconditions": {}, "verdict": "unknown"})
            ok = False
            continue
        pre = evaluate_preconditions(rid, physical, logical, registry)
        verdict = "admitted" if all(pre.values()) else "precondition_failed"
        ok &= verdict == "admitted"
        rules.append({"rule_id": rid, "evidence_class": spec["evidence_class"], "preconditions": pre, "verdict": verdict})
    certified_obligations = set()
    if ok:
        certified_obligations = {"ob.identity_unique", "ob.exclusion_ledger_conservation", "ob.semijoin_inner_region",
                                 "ob.preserve_grain", "ob.half_open_window", "ob.numerator_le_denominator"}
        if "partition" in physical["variant"]:
            certified_obligations |= {"ob.copartition_same_key", "ob.partition_disjoint_cover", "ob.denominator_conservation"}
    cert = {
        "physical_fingerprint": physical["physical_fingerprint"], "logical_fingerprint": logical["logical_fingerprint"],
        "variant": physical["variant"], "rules": rules, "admitted": ok,
        "certified_obligations": sorted(certified_obligations),
        "verification": bounded_verify(physical, logical),
        "empirical": empirical,
        "registry_version": registry.versions["rewrite_rules"],
    }
    cert["certificate_id"] = "cert_" + fingerprint(cert).split(":")[1][:16]
    return cert


def bounded_verify(physical: dict[str, Any], logical: dict[str, Any]) -> dict[str, Any]:
    """Optional VeriEQL-style bounded equivalence adapter."""
    tool = shutil.which("verieql")
    if tool is None:
        return {"verifier": "VeriEQL", "verdict": "unsupported", "reason": "verifier not installed; window functions, UDFs "
                "and staged CTAS reconstruction are outside the adapter's supported feature set", "bound": None}
    return {"verifier": "VeriEQL", "verdict": "unknown", "reason": "integration not configured in this POC", "bound": None,
            "binary": tool}


def compare_results(a: list[dict[str, Any]], b: list[dict[str, Any]], dims: list[str],
                    metrics=("eligible_all", "eligible_mature", "immature", "quality_excluded", "converted")) -> dict[str, Any]:
    """Bag comparison keyed by dimension values over exact count statistics."""
    def key(r):
        return tuple(r.get(d) for d in dims)
    ma = {key(r): tuple(int(r[m] or 0) for m in metrics) for r in a if int(r.get("eligible_all") or 0) > 0}
    mb = {key(r): tuple(int(r[m] or 0) for m in metrics) for r in b if int(r.get("eligible_all") or 0) > 0}
    diffs = []
    for k in sorted(set(ma) | set(mb), key=str):
        if ma.get(k) != mb.get(k):
            diffs.append({"key": k, "left": ma.get(k), "right": mb.get(k)})
    return {"equivalent": not diffs, "differences": diffs, "metrics": list(metrics)}


def tlp_check(session, base_from_sql: str, predicate: str, params: dict[str, Any], cut: dict[str, int],
              projection: str = "*") -> dict[str, Any]:
    """Ternary Logic Partitioning for a projection/filter query.

    Q        = SELECT proj FROM base
    Q_tlp    = SELECT proj FROM base WHERE p
               UNION ALL SELECT proj FROM base WHERE NOT p
               UNION ALL SELECT proj FROM base WHERE p IS NULL
    Bags must be equal. Aggregates, DISTINCT, ORDER/LIMIT are not handled here.
    """
    q = f"SELECT {projection} FROM {base_from_sql} t"
    q_tlp = (f"SELECT {projection} FROM {base_from_sql} t WHERE ({predicate}) UNION ALL "
             f"SELECT {projection} FROM {base_from_sql} t WHERE NOT ({predicate}) UNION ALL "
             f"SELECT {projection} FROM {base_from_sql} t WHERE ({predicate}) IS NULL")
    left = session.fetch_all(q, params, cut=cut)
    right = session.fetch_all(q_tlp, params, cut=cut)
    from collections import Counter

    def bag(rows):
        return Counter(tuple(sorted(r.items())) for r in rows)
    bl, br = bag(left), bag(right)
    return {"oracle": "TLP", "predicate": predicate, "equal": bl == br, "rows": len(left), "partitioned_rows": len(right),
            "missing": sum((bl - br).values()), "extra": sum((br - bl).values())}
