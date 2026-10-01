"""Runtime validation of declared obligations (sections 9, 11, 13).

Node assertions run right after a stage completes (a grain violation blocks
publication immediately). Final assertions run in the validate node against
the final relation and, where present, stage relations. Each verdict records
the observed value so receipts can show evidence, not just pass/fail.
"""

from __future__ import annotations

from typing import Any

from .engines.base import validate_relation_name


def _scalar(session, sql: str, params: dict | None = None) -> Any:
    rows = session.fetch_all(sql, params or {})
    if not rows:
        return None
    return next(iter(rows[0].values()))


def node_assertions(session, node: dict[str, Any], relation: str) -> dict[str, dict[str, Any]]:
    validate_relation_name(relation)
    out: dict[str, dict[str, Any]] = {}
    for a in node.get("assertions", []):
        if a == "a.grain_unique_cohort" or a == "a.grain_unique_outcome":
            v = _scalar(session, f"SELECT COUNT(*) - COUNT(DISTINCT opportunity_id) AS v FROM {relation}")
            out[a] = {"passed": v == 0, "value": v, "detail": "duplicate opportunity keys"}
        elif a == "a.grain_unique_binds":
            v = _scalar(session, f"SELECT COUNT(*) - COUNT(DISTINCT bind_event_id) AS v FROM {relation}")
            out[a] = {"passed": v == 0, "value": v, "detail": "duplicate bind event keys"}
        elif a == "a.partition_cover":
            n = int(node["params"]["n_partitions"])
            v = _scalar(session, f"SELECT COUNT(*) AS v FROM {relation} WHERE partition_id IS NULL OR partition_id < 0 OR partition_id >= :n", {"n": n})
            out[a] = {"passed": v == 0, "value": v, "detail": f"rows outside partitions [0,{n})"}
    return out


def final_assertions(session, final_relation: str, cutoff: str, stage_relations: dict[str, str],
                     assertions: list[str]) -> dict[str, dict[str, Any]]:
    validate_relation_name(final_relation)
    out: dict[str, dict[str, Any]] = {}
    rows = session.fetch_all(f"SELECT * FROM {final_relation}")
    sums = {k: sum(int(r.get(k) or 0) for r in rows)
            for k in ("eligible_all", "eligible_mature", "immature", "quality_excluded", "converted", "grain_violations", "window_violations")}
    for a in assertions:
        if a == "a.final_grain_unique":
            dims = [k for k in (rows[0].keys() if rows else []) if k in ("product_version", "channel")]
            keys = [tuple(r[d] for d in dims) for r in rows]
            ok = sums["grain_violations"] == 0 and len(keys) == len(set(keys))
            out[a] = {"passed": ok, "value": {"grain_violations": sums["grain_violations"], "rows": len(rows)},
                      "detail": "one row per output grain; one opportunity per cohort row"}
            ogv = [r.get("outcome_grain_violations") for r in rows if r.get("outcome_grain_violations") is not None]
            if ogv and "cohort" not in stage_relations:
                # fused/partitioned: the outcome relation preserves cohort rows 1:1, so a duplicate-free
                # outcome (max per partition) evidences both cohort and outcome grain obligations
                v = {"passed": max(ogv) == 0, "value": max(ogv), "detail": "duplicate opportunity keys in the outcome relation "
                     "(observed inside the fused/partitioned statement; max over partitions)"}
                out.setdefault("a.grain_unique_outcome", v)
                out.setdefault("a.grain_unique_cohort", dict(v))
            elif ogv:
                out.setdefault("a.grain_unique_outcome", {"passed": max(ogv) == 0, "value": max(ogv),
                                                          "detail": "duplicate opportunity keys in the outcome relation"})
        elif a == "a.conservation":
            diff = sums["eligible_all"] - (sums["eligible_mature"] + sums["immature"] + sums["quality_excluded"])
            detail = {"eligible_all": sums["eligible_all"], "ledger_sum": sums["eligible_mature"] + sums["immature"] + sums["quality_excluded"]}
            ok = diff == 0
            if "cohort" in stage_relations:
                n = _scalar(session, f"SELECT COUNT(*) AS v FROM {validate_relation_name(stage_relations['cohort'])}")
                detail["cohort_rows"] = n
                ok = ok and n == sums["eligible_all"]
            out[a] = {"passed": ok, "value": detail, "detail": "eligible_all = mature + immature + quality_excluded"}
        elif a == "a.num_le_den":
            bad = sum(1 for r in rows if int(r["converted"] or 0) > int(r["eligible_mature"] or 0))
            out[a] = {"passed": bad == 0, "value": bad, "detail": "rows with converted > eligible_mature"}
        elif a == "a.no_future_knowledge":
            mk = max((r["max_knowledge_at"] for r in rows if r.get("max_knowledge_at")), default=None)
            mb = max((r["max_bind_knowledge_at"] for r in rows if r.get("max_bind_knowledge_at")), default=None)
            ok = (mk is None or mk <= cutoff) and (mb is None or mb <= cutoff)
            out[a] = {"passed": ok, "value": {"max_quote_knowledge_at": mk, "max_bind_knowledge_at": mb, "cutoff": cutoff},
                      "detail": "no record known after the cutoff contributed"}
        elif a == "a.window_respected":
            out[a] = {"passed": sums["window_violations"] == 0, "value": sums["window_violations"],
                      "detail": "converted outcomes whose first bind lies outside the recipe interval"}
        elif a == "a.partition_conservation":
            n = None
            if "cohort" in stage_relations:
                n = _scalar(session, f"SELECT COUNT(*) AS v FROM {validate_relation_name(stage_relations['cohort'])}")
            out[a] = {"passed": n == sums["eligible_all"], "value": {"cohort_rows": n, "merged_eligible_all": sums["eligible_all"]},
                      "detail": "merged partition denominators equal the canonical cohort"}
    return out


def stage_registration(store, execution_id: str, principal: str, relations: list[str]) -> dict[str, Any]:
    missing = []
    for rel in relations:
        row = store.one("SELECT * FROM stages WHERE relation=?", (rel,))
        if not row or row["owner_principal"] != principal or not row["expires_at"] or row["execution_id"] != execution_id:
            missing.append(rel)
    return {"passed": not missing, "value": {"stages": len(relations), "unregistered": missing},
            "detail": "stage private to producing principal with expiry metadata"}
