"""Physical resolution statistics and useful mini queries (section 6).

Existing profiles are reused before any probe. A probe is issued only after
semantic confirmation, within the discovery budget and probe count, and only
when its expected decision benefit exceeds its cost (it could change the
physical variant, a materialization boundary or the estimate). Every statistic
records source cut, predicate scope, collection time, exact/approximate label
and access scope. Registered (catalog) estimates are labelled approximate.
"""

from __future__ import annotations

from typing import Any

from .canonical import fingerprint
from .sqlgen import Dialect, cohort_sql, linked_binds_sql
from .store import Store, dumps, loads

PROBES = {
    "cohort_size": {"benefit": "cohort size drives variant choice and estimates", "est_seconds": 0.5},
    "duplicate_canonical_key": {"benefit": "verifies one canonical row per opportunity before staging", "est_seconds": 0.5},
    "bind_multiplicity": {"benefit": "bind fan-out and linked-row volume decide partitioning", "est_seconds": 0.6},
    "heavy_hitters": {"benefit": "heavy keys decide skew isolation", "est_seconds": 0.6},
}


def registered_estimates(registry, rec) -> dict[str, Any]:
    q = registry.port("quote_fdp/quoted_opportunities_v2")["statistics"]
    b = registry.port("policy_fdp/known_bind_events_v2")["statistics"]
    months = 1.0
    if rec.intent.cohort:
        from datetime import date
        days = (date.fromisoformat(rec.intent.cohort.end_exclusive) - date.fromisoformat(rec.intent.cohort.start)).days
        months = max(days / 30.0, 0.03)
    cohort = q["rows_per_month_estimate"] * months / q["branches_per_submission_mean"]
    return {"cohort_rows": cohort, "linked_bind_rows": cohort * 0.45, "branches_per_opportunity": q["branches_per_submission_mean"],
            "bind_multiplicity_p95": b["binds_per_opportunity_p95"], "bind_multiplicity_max": 4, "output_rows": 6,
            "uncertainty": 4.0, "source": "registered_catalog_estimate", "exact": False}


class Profiler:
    def __init__(self, store: Store, broker, clock):
        self.store = store
        self.broker = broker
        self.clock = clock

    def _key(self, name: str, scope_hash: str, cut_fp: str, access_key: str) -> str:
        return fingerprint({"name": name, "scope": scope_hash, "cut": cut_fp, "access": access_key})

    def lookup(self, name: str, scope_hash: str, cut_fp: str, access_key: str) -> dict[str, Any] | None:
        row = self.store.one("SELECT * FROM profiles WHERE profile_key=?", (self._key(name, scope_hash, cut_fp, access_key),))
        return {**loads(row["value_json"]), "collected_at": row["collected_at"], "reused": True} if row else None

    def probe_sql(self, name: str, dialect: Dialect, identity: str) -> str:
        cohort = cohort_sql(dialect, identity, False)
        if name == "cohort_size":
            return f"SELECT cohort_class, COUNT(*) AS n FROM ({cohort}) GROUP BY cohort_class"
        if name == "duplicate_canonical_key":
            return f"SELECT COUNT(*) - COUNT(DISTINCT opportunity_id) AS dup FROM ({cohort})"
        linked = linked_binds_sql(dialect, identity, "cohort", False)
        base = f"WITH cohort AS ({cohort}), linked AS ({linked}) "
        if name == "bind_multiplicity":
            return base + "SELECT n, COUNT(*) AS keys FROM (SELECT opportunity_id, COUNT(*) AS n FROM linked GROUP BY opportunity_id) GROUP BY n ORDER BY n"
        if name == "heavy_hitters":
            return base + "SELECT opportunity_id, COUNT(*) AS n FROM linked GROUP BY opportunity_id ORDER BY n DESC, opportunity_id LIMIT 5"
        raise KeyError(name)

    def collect(self, session, logical: dict[str, Any], cut, access, discovery_target: float, max_probes: int,
                registered: dict[str, Any], force_probes: bool = False) -> dict[str, Any]:
        """Return statistics plus a per-probe decision log."""
        scope_hash = fingerprint(logical["params"] | {"identity": logical["algebra"]["identity"]})
        stats = dict(registered)
        log: list[dict[str, Any]] = []
        issued = 0
        spent = 0.0
        dialect = Dialect(session.engine.dialect if hasattr(session, "engine") else "sqlite")
        identity = logical["algebra"]["identity"]
        for name, spec in PROBES.items():
            existing = self.lookup(name, scope_hash, cut.fingerprint, access.reuse_key)
            if existing:
                log.append({"probe": name, "decision": "reused_existing_profile", "collected_at": existing["collected_at"]})
                self._apply(stats, name, existing)
                continue
            # value of information: registered estimates are approximate; probes
            # matter when the decision could flip (uncertainty band spans thresholds).
            value = 1.0 if (registered.get("uncertainty", 1) >= 2 or force_probes) else 0.0
            adm = self.broker.admit_probe(discovery_target - spent, spec["est_seconds"])
            if not adm["admitted"] and adm.get("alternatives"):
                # prefer a smaller eligible resource class over no probe
                adm = self.broker.admit_probe(discovery_target - spent, spec["est_seconds"], adm["alternatives"][0]) | {
                    "downgraded_from": adm["warehouse_class"]}
            if issued >= max_probes:
                log.append({"probe": name, "decision": "skipped", "reason": "probe count limit"})
                continue
            if value <= 0:
                log.append({"probe": name, "decision": "skipped", "reason": "no decision benefit"})
                continue
            if not adm["admitted"]:
                log.append({"probe": name, "decision": "not_admitted", "reason": adm["reason"], "budget": adm})
                continue
            sql = self.probe_sql(name, dialect, identity)
            rows = session.fetch_all(sql, logical["params"], cut=cut.seq_by_port)
            self.broker.pool.touch(adm["warehouse_class"])
            spent += adm["required_credits"]
            issued += 1
            value_obj = self._summarize(name, rows)
            value_obj.update({"exact": True, "source_cut": cut.fingerprint, "predicate_scope": scope_hash,
                              "staleness_policy": "valid for this source cut only"})
            self.store.x("INSERT OR REPLACE INTO profiles VALUES (?,?,?,?,?,?,?,?,?)", (
                self._key(name, scope_hash, cut.fingerprint, access.reuse_key), "quote+bind", cut.fingerprint, scope_hash, name,
                dumps(value_obj), 1, self.clock.now_z(), access.reuse_key))
            self._apply(stats, name, value_obj)
            log.append({"probe": name, "decision": "issued", "benefit": spec["benefit"], "credits": adm["required_credits"],
                        "result": value_obj})
        exact = any(e["decision"] in ("issued", "reused_existing_profile") for e in log)
        if exact:
            stats["uncertainty"] = 1.5
            stats["source"] = "exact_profile"
            stats["exact"] = True
        stats["probe_log"] = log
        stats["discovery_credits_spent"] = round(spent, 6)
        return stats

    @staticmethod
    def _summarize(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if name == "cohort_size":
            by = {r["cohort_class"]: r["n"] for r in rows}
            return {"cohort_rows": sum(by.values()), "by_class": by}
        if name == "duplicate_canonical_key":
            return {"duplicates": rows[0]["dup"] if rows else 0}
        if name == "bind_multiplicity":
            total_keys = sum(r["keys"] for r in rows)
            total_rows = sum(r["n"] * r["keys"] for r in rows)
            cum, p50, p95 = 0, None, None
            for r in rows:
                cum += r["keys"]
                if p50 is None and cum >= 0.5 * total_keys:
                    p50 = r["n"]
                if p95 is None and cum >= 0.95 * total_keys:
                    p95 = r["n"]
            return {"linked_bind_rows": total_rows, "keys_with_binds": total_keys, "p50": p50, "p95": p95,
                    "max": max((r["n"] for r in rows), default=0)}
        if name == "heavy_hitters":
            return {"top": rows}
        return {"rows": rows}

    @staticmethod
    def _apply(stats: dict[str, Any], name: str, v: dict[str, Any]) -> None:
        if name == "cohort_size":
            stats["cohort_rows"] = v["cohort_rows"]
            stats["cohort_by_class"] = v["by_class"]
        elif name == "duplicate_canonical_key":
            stats["canonical_duplicates"] = v["duplicates"]
        elif name == "bind_multiplicity":
            stats["linked_bind_rows"] = v["linked_bind_rows"]
            stats["bind_multiplicity_p50"] = v["p50"]
            stats["bind_multiplicity_p95"] = v["p95"]
            stats["bind_multiplicity_max"] = v["max"]
        elif name == "heavy_hitters":
            stats["heavy_hitters"] = v["top"]
