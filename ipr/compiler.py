"""Typed information-plan compiler (section 7).

Two representations:

* logical IR — what must be computed: approved operators, input/output grains,
  recipe bindings, temporal meaning, declared obligations. Its fingerprint is
  independent of physical choices.
* physical plan — where/how: SQL per node, dependencies, materialization,
  partitions, resource class, envelopes, assertions, checkpoints.

The compiler is deliberately bounded to the installed quote-conversion
algebra; it synthesizes graphs from validated intent fields and approved
operators and never accepts agent-authored SQL.
"""

from __future__ import annotations

import copy
from typing import Any

import sqlglot
from sqlglot import exp

from . import COMPILER_VERSION
from .canonical import fingerprint
from .engines.base import HASH_VERSION
from .intent import IntentRecord
from .sqlgen import (
    SQLITE_FUNCTIONS,
    Dialect,
    aggregate_select,
    cohort_sql,
    linked_binds_sql,
    merge_sql,
    outcome_sql,
)

QUOTE_PORT_REF = "quote_fdp/quoted_opportunities_v2"
BIND_PORT_REF = "policy_fdp/known_bind_events_v2"
VARIANTS = ("fused", "staged", "partitioned")


class CompileError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Logical IR
# ---------------------------------------------------------------------------

def bound_params(rec: IntentRecord, recipe: dict[str, Any]) -> dict[str, Any]:
    i = rec.intent
    if not (i.cohort and i.window and i.knowledge_cutoff and i.scope.get("state") and i.scope.get("lob")):
        raise CompileError("intent is missing required bound parameters")
    return {
        "state": i.scope["state"], "lob": i.scope["lob"], "cohort_start": i.cohort.start,
        "cohort_end": i.cohort.end_exclusive, "window_days": int(i.window.value), "cutoff": i.knowledge_cutoff,
        "tz": recipe["algebra"]["timezone"],
    }


def build_logical(rec: IntentRecord, recipe: dict[str, Any], registry) -> dict[str, Any]:
    dims = list(rec.intent.dimensions)
    alg = recipe["algebra"]
    temporal = {"effective_anchor": recipe["definitions"]["window"]["anchor"], "knowledge_cutoff_ref": "intent.knowledge_cutoff",
                "timezone_ref": "intent.business_timezone", "interval": recipe["definitions"]["window"]["interval"]}
    op = registry.operators
    nodes = [
        {"id": "scan_quotes", "operator": "scoped_scan", "inputs": [], "port": QUOTE_PORT_REF, "input_grains": [],
         "output_grain": ["quote_branch_id"], "obligation_declaration_ref": f"operator_registry/scoped_scan/v{op['scoped_scan']['version']}"},
        {"id": "scan_binds", "operator": "scoped_scan", "inputs": [], "port": BIND_PORT_REF, "input_grains": [],
         "output_grain": ["bind_event_id"], "obligation_declaration_ref": f"operator_registry/scoped_scan/v{op['scoped_scan']['version']}"},
        {"id": "canonical_cohort", "operator": "canonical_event_selection", "inputs": ["scan_quotes"], "input_grains": [["quote_branch_id"]],
         "output_grain": ["opportunity_id"], "identity": alg["identity"], "temporal": temporal,
         "rule": recipe["definitions"]["attribution_rule"], "obligation_declaration_ref": "operator_registry/canonical_event_selection/v2"},
        {"id": "linked_binds", "operator": "key_reduction", "inputs": ["scan_binds", "scan_quotes", "canonical_cohort"],
         "input_grains": [["bind_event_id"], ["quote_branch_id"], ["opportunity_id"]], "output_grain": ["bind_event_id"],
         "join": {"keys": ["opportunity_id"], "relationship": "many_to_one", "semantics": "semijoin_mature_cohort_keys"},
         "obligation_declaration_ref": "operator_registry/key_reduction/v1"},
        {"id": "quote_outcome", "operator": "window_exists", "inputs": ["canonical_cohort", "linked_binds"],
         "input_grains": [["opportunity_id"], ["bind_event_id"]], "output_grain": ["opportunity_id"],
         "join": {"keys": ["opportunity_id"], "relationship": "one_to_many", "semantics": f"exists_within_{alg['window_kind']}",
                  "reduction": "one_outcome_per_opportunity"}, "temporal": temporal,
         "obligation_declaration_ref": "operator_registry/window_exists/v2"},
        {"id": "metric_stats", "operator": "aggregate_sufficient_stats", "inputs": ["quote_outcome"], "input_grains": [["opportunity_id"]],
         "output_grain": dims, "statistics": recipe["sufficient_statistics"],
         "obligation_declaration_ref": "operator_registry/aggregate_sufficient_stats/v1"},
    ]
    edges = [{"from": src, "to": n["id"]} for n in nodes for src in n["inputs"]]
    logical = {
        "kind": "logical_information_ir", "compiler_version": COMPILER_VERSION,
        "recipe": registry.recipe_ref(recipe), "algebra": alg, "params": bound_params(rec, recipe),
        "dimensions": dims, "bindings": [QUOTE_PORT_REF, BIND_PORT_REF], "nodes": nodes, "edges": edges,
        "outputs": [{"operator": "publish_information_set"}, {"operator": "present"}],
    }
    logical["logical_fingerprint"] = fingerprint({k: v for k, v in logical.items() if k != "compiler_version"})
    return logical


def check_edges(logical: dict[str, Any]) -> list[str]:
    """Edge grain compatibility: each consumer's declared input grain equals its producer's output grain."""
    problems = []
    nodes = {n["id"]: n for n in logical["nodes"]}
    for n in logical["nodes"]:
        for idx, src in enumerate(n["inputs"]):
            if src not in nodes:
                problems.append(f"{src}->{n['id']}: dangling")
                continue
            if n["input_grains"][idx] != nodes[src]["output_grain"]:
                problems.append(f"{src}->{n['id']}: grain {nodes[src]['output_grain']} != accepted {n['input_grains'][idx]}")
    return problems


# ---------------------------------------------------------------------------
# Physical plans
# ---------------------------------------------------------------------------

def _resource_class(rows: float) -> str:
    if rows < 2e5:
        return "XSMALL"
    if rows < 5e6:
        return "SMALL"
    if rows < 5e7:
        return "MEDIUM"
    return "LARGE"


def _envelope(est: float, uncertainty: float = 3.0) -> list[int]:
    return [int(max(0, est / uncertainty)), int(est * uncertainty) + 10]


def build_physical(logical: dict[str, Any], variant: str, stats: dict[str, Any], dialect: str = "sqlite",
                   n_partitions: int = 4) -> dict[str, Any]:
    if variant not in VARIANTS:
        raise CompileError(f"unknown variant {variant}")
    d = Dialect(dialect)
    identity = logical["algebra"]["identity"]
    dims = logical["dimensions"]
    params = dict(logical["params"])
    cohort_est = float(stats.get("cohort_rows", 1000))
    binds_est = float(stats.get("linked_bind_rows", cohort_est * 0.4))
    out_est = float(stats.get("output_rows", 6))
    nodes: list[dict[str, Any]] = []

    def node(node_id, operator, logical_nodes, sql, deps, est_rows, grain, *, assertions=(), checkpoint=False, extra_params=None,
             partition=None, materialization="stage"):
        nodes.append({
            "id": node_id, "kind": "sql_ctas", "operator": operator, "logical_nodes": logical_nodes, "sql": sql.strip(),
            "params": {**params, **(extra_params or {})}, "depends_on": list(deps), "materialization": materialization,
            "expected_grain": grain, "assertions": list(assertions), "checkpoint": checkpoint,
            "envelope": {"rows": _envelope(est_rows), "estimate": int(est_rows)}, "resource_class": _resource_class(est_rows),
            "partition": partition,
        })

    if variant == "fused":
        sql = ("WITH cohort AS (" + cohort_sql(d, identity, False) + "\n), linked AS (" +
               linked_binds_sql(d, identity, "cohort", False) + "\n), outcome AS (" + outcome_sql("cohort", "linked") +
               "\n)" + aggregate_select(dims, "outcome"))
        node("fused_metric", "aggregate_sufficient_stats", ["canonical_cohort", "linked_binds", "quote_outcome", "metric_stats"],
             sql, [], out_est, dims, assertions=["a.final_grain_unique", "a.conservation", "a.num_le_den", "a.no_future_knowledge",
                                                  "a.window_respected"])
        final = "fused_metric"
    else:
        partitioned = variant == "partitioned"
        extra = {"n_partitions": n_partitions} if partitioned else {}
        node("cohort", "canonical_event_selection", ["canonical_cohort"], cohort_sql(d, identity, partitioned), [], cohort_est,
             ["opportunity_id"], assertions=["a.grain_unique_cohort"] + (["a.partition_cover"] if partitioned else []),
             checkpoint=True, extra_params=extra)
        node("linked_binds", "key_reduction", ["linked_binds"], linked_binds_sql(d, identity, "{{rel:cohort}}", partitioned),
             ["cohort"], binds_est, ["bind_event_id"], assertions=["a.grain_unique_binds"], checkpoint=True, extra_params=extra)
        if not partitioned:
            node("outcome", "window_exists", ["quote_outcome"], outcome_sql("{{rel:cohort}}", "{{rel:linked_binds}}"),
                 ["cohort", "linked_binds"], cohort_est, ["opportunity_id"], assertions=["a.grain_unique_outcome"])
            node("metric_stats", "aggregate_sufficient_stats", ["metric_stats"], aggregate_select(dims, "{{rel:outcome}}"),
                 ["outcome"], out_est, dims, assertions=["a.final_grain_unique", "a.conservation", "a.num_le_den",
                                                          "a.no_future_knowledge", "a.window_respected"])
            final = "metric_stats"
        else:
            final = _partition_suffix(nodes, params, dims, n_partitions, cohort_est, out_est, labelled=True, dialect=d)
    nodes.append({"id": "validate", "kind": "validate", "operator": "assert", "depends_on": [n["id"] for n in nodes],
                  "final_node": final, "assertions": ["a.stage_registered"], "materialization": None, "checkpoint": False,
                  "resource_class": "XSMALL", "envelope": None, "params": {"cutoff": params["cutoff"]}})
    nodes.append({"id": "publish", "kind": "publish", "operator": "publish_information_set", "depends_on": ["validate"],
                  "final_node": final, "materialization": None, "checkpoint": False, "resource_class": None, "envelope": None, "params": {}})
    nodes.append({"id": "present", "kind": "present", "operator": "present", "depends_on": ["publish"],
                  "final_node": final, "materialization": None, "checkpoint": False, "resource_class": None, "envelope": None, "params": {}})
    phys = {
        "kind": "physical_plan", "variant": variant, "dialect": dialect, "compiler_version": COMPILER_VERSION,
        "logical_fingerprint": logical["logical_fingerprint"], "n_partitions": n_partitions if variant == "partitioned" else None,
        "hash_version": HASH_VERSION if variant == "partitioned" else None, "final_node": final, "nodes": nodes,
        "rewrite_rules": rewrite_rules_for(variant),
    }
    validate_graph(phys)
    for n in phys["nodes"]:
        if n["kind"] == "sql_ctas":
            validate_sql(n["sql"], dialect)
    phys["physical_fingerprint"] = fingerprint({k: v for k, v in phys.items() if k != "compiler_version"})
    return phys


def _partition_suffix(nodes: list[dict[str, Any]], params: dict[str, Any], dims: list[str], n: int, cohort_est: float,
                      out_est: float, labelled: bool, dialect: Dialect, suffix_tag: str = "") -> str:
    """Per-partition exact window reduction + aggregation, then additive merge."""
    refs = []
    for p in range(n):
        if labelled:
            fc, fk = " AND c.partition_id = :partition", " AND k.partition_id = :partition"
        else:
            fc = f" AND {dialect.hash_mod('c.opportunity_id', ':n_partitions')} = :partition"
            fk = f" AND {dialect.hash_mod('k.opportunity_id', ':n_partitions')} = :partition"
        sql = "WITH outcome AS (" + outcome_sql("{{rel:cohort}}", "{{rel:linked_binds}}", fc, fk) + "\n)" + aggregate_select(dims, "outcome")
        nid = f"window_reduce{suffix_tag}_p{p}"
        nodes.append({
            "id": nid, "kind": "sql_ctas", "operator": "window_exists", "logical_nodes": ["quote_outcome", "metric_stats"],
            "sql": sql.strip(), "params": {**params, "n_partitions": n, "partition": p}, "depends_on": ["cohort", "linked_binds"],
            "materialization": "stage", "expected_grain": dims, "assertions": [], "checkpoint": False,
            "envelope": {"rows": _envelope(out_est), "estimate": int(out_est)}, "resource_class": _resource_class(cohort_est / n),
            "partition": p,
        })
        refs.append(nid)
    mid = f"merge{suffix_tag}"
    nodes.append({
        "id": mid, "kind": "sql_ctas", "operator": "union_disjoint_partitions", "logical_nodes": ["metric_stats"],
        "sql": merge_sql(dims, [f"{{{{rel:{r}}}}}" for r in refs]).strip(), "params": {}, "depends_on": refs,
        "materialization": "stage", "expected_grain": dims,
        "assertions": ["a.final_grain_unique", "a.conservation", "a.num_le_den", "a.no_future_knowledge", "a.window_respected",
                       "a.partition_conservation"],
        "checkpoint": False, "envelope": {"rows": _envelope(out_est), "estimate": int(out_est)}, "resource_class": "XSMALL", "partition": None,
    })
    return mid


def replan_suffix(physical: dict[str, Any], completed: set[str], new_partitions: int) -> dict[str, Any]:
    """Return a new physical plan version whose unstarted suffix after the
    cohort/linked_binds materialization boundary is a co-partitioned window
    reduction with ``new_partitions`` partitions (rw.copartition_window_reduction_v1
    / rw.combine_partitions_v1). Completed nodes are kept immutable."""
    if not {"cohort", "linked_binds"} <= completed:
        raise CompileError("suffix replanning requires the cohort/linked_binds boundary to be complete")
    keep = [n for n in physical["nodes"] if n["id"] in ("cohort", "linked_binds")]
    params = {k: v for k, v in keep[0]["params"].items() if k not in ("n_partitions",)}
    final_node = next(n for n in physical["nodes"] if n["id"] == physical["final_node"])
    dims = final_node["expected_grain"]
    cohort_est = keep[0]["envelope"]["estimate"]
    out_est = final_node["envelope"]["estimate"]
    version = int(physical.get("adaptation_version", 0)) + 1
    tag = f"_v{version}"
    nodes = copy.deepcopy(keep)
    final = _partition_suffix(nodes, params, dims, new_partitions, cohort_est, out_est, labelled=False,
                              dialect=Dialect(physical["dialect"]), suffix_tag=tag)
    for kind, op in (("validate", "assert"), ("publish", "publish_information_set"), ("present", "present")):
        old = next(n for n in physical["nodes"] if n["id"] == kind)
        new = copy.deepcopy(old)
        new["final_node"] = final
        if kind == "validate":
            new["depends_on"] = [n["id"] for n in nodes]
        nodes.append(new)
    new_plan = copy.deepcopy(physical)
    new_plan.update({"nodes": nodes, "final_node": final, "variant": f"{physical['variant']}+adapted_partitioned",
                     "n_partitions": new_partitions, "hash_version": HASH_VERSION, "adaptation_version": version,
                     "rewrite_rules": sorted(set(physical["rewrite_rules"]) | {"rw.copartition_window_reduction_v1", "rw.combine_partitions_v1"})})
    validate_graph(new_plan)
    for n in new_plan["nodes"]:
        if n["kind"] == "sql_ctas":
            validate_sql(n["sql"], new_plan["dialect"])
    new_plan["physical_fingerprint"] = fingerprint({k: v for k, v in new_plan.items() if k != "compiler_version"})
    return new_plan


def rewrite_rules_for(variant: str) -> list[str]:
    rules = ["rw.semijoin_key_reduction_v1"]
    if variant in ("staged", "partitioned"):
        rules.append("rw.fuse_staged_v1")
    if variant == "partitioned":
        rules.append("rw.copartition_window_reduction_v1")
    return sorted(rules)


# ---------------------------------------------------------------------------
# Graph and SQL validation
# ---------------------------------------------------------------------------

def validate_graph(phys: dict[str, Any]) -> None:
    ids = [n["id"] for n in phys["nodes"]]
    if len(ids) != len(set(ids)):
        raise CompileError("duplicate node ids")
    known = set(ids)
    for n in phys["nodes"]:
        for dep in n["depends_on"]:
            if dep not in known:
                raise CompileError(f"{n['id']} depends on unknown {dep}")
    # Kahn topological sort -> acyclic & schedulable
    indeg = {i: 0 for i in ids}
    for n in phys["nodes"]:
        indeg[n["id"]] = len(n["depends_on"])
    ready = [i for i, k in indeg.items() if k == 0]
    order = []
    children = {i: [] for i in ids}
    for n in phys["nodes"]:
        for dep in n["depends_on"]:
            children[dep].append(n["id"])
    while ready:
        cur = ready.pop()
        order.append(cur)
        for c in children[cur]:
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)
    if len(order) != len(ids):
        raise CompileError("physical graph has a cycle")
    if phys["final_node"] not in known or "present" not in known:
        raise CompileError("graph incomplete with respect to required outputs")
    if len(ids) > 64:
        raise CompileError("graph exceeds node bound")
    phys["topological_order"] = order


FORBIDDEN = (exp.Drop, exp.Delete, exp.Insert, exp.Update, exp.Alter, exp.Command, exp.Pragma, exp.Merge)


def validate_sql(sql: str, dialect: str = "sqlite") -> dict[str, Any]:
    """AST validation: single read-only SELECT over allowlisted relations and functions."""
    placeholder_sql = sql.replace("{{port:", "__port__").replace("{{rel:", "__rel__").replace("}}", "")
    placeholder_sql = placeholder_sql.replace("/", "__")
    try:
        trees = sqlglot.parse(placeholder_sql, read="snowflake" if dialect == "snowflake" else "sqlite")
    except sqlglot.errors.ParseError as exc:
        raise CompileError(f"SQL does not parse: {exc}") from exc
    if len(trees) != 1 or trees[0] is None:
        raise CompileError("exactly one statement is allowed")
    tree = trees[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.With)) and not tree.find(exp.Select):
        raise CompileError("only SELECT statements are compiled")
    for bad in FORBIDDEN:
        if tree.find(bad):
            raise CompileError(f"disallowed operation {bad.__name__}")
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        name = t.name
        if name in ctes or name.startswith("__port__") or name.startswith("__rel__"):
            continue
        raise CompileError(f"relation {name!r} is not an allowlisted port, stage or CTE")
    if dialect == "sqlite":
        for f in tree.find_all(exp.Anonymous):
            if f.name.lower() not in SQLITE_FUNCTIONS:
                raise CompileError(f"function {f.name} not allowlisted")
    return {"valid": True, "ctes": sorted(ctes)}
