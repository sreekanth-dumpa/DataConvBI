"""Coverage-aware information-set cache (section 13).

Exact reuse needs the same coverage key (semantic payload without output
dimensions), the same output dimensions, the same source cut and the same
access reuse key. Derived reuse permits only admitted algebraic
transformations: summing additive counts over removed dimensions
(rw.derived_rollup_sum_v1) and recomputing the ratio — never averaging rates.

Every read re-authorizes against current access. Entries are only published
for complete, validated results. Pinned entries are immutable; latest-mode
entries carry a freshness deadline. The original producer receipt is kept and
a separate cache-operation receipt is written for each reuse.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .canonical import fingerprint, parse_ts, to_utc_z
from .intent import IntentRecord, coverage_key, coverage_payload, semantic_hash
from .store import Store, dumps, loads

COUNT_FIELDS = ("eligible_all", "eligible_mature", "immature", "quality_excluded", "converted")


@dataclass
class CacheDecision:
    outcome: str  # exact | derived | miss
    entry: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    rule: str | None = None
    rejected: list[dict[str, Any]] = field(default_factory=list)
    reauthorization: str | None = None
    availability: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "information_set_id": self.entry["information_set_id"] if self.entry else None,
                "rule": self.rule, "rejected": self.rejected, "reauthorization": self.reauthorization,
                "availability": self.availability, "rows": len(self.rows)}


def rate(converted: int, mature: int) -> float | None:
    return None if mature == 0 else converted / mature


def rollup(rows: list[dict[str, Any]], dims: list[str]) -> list[dict[str, Any]]:
    agg: dict[tuple, dict[str, int]] = {}
    for r in rows:
        key = tuple(r[d] for d in dims)
        a = agg.setdefault(key, {k: 0 for k in COUNT_FIELDS})
        for k in COUNT_FIELDS:
            a[k] += int(r[k] or 0)
    out = []
    for key in sorted(agg, key=lambda k: tuple(str(x) for x in k)):
        row = dict(zip(dims, key))
        row.update(agg[key])
        row["conversion_rate"] = rate(row["converted"], row["eligible_mature"])
        out.append(row)
    return out


def _diff_coverage(a: dict[str, Any], b: dict[str, Any], prefix: str = "") -> list[str]:
    diffs = []
    for k in sorted(set(a) | set(b)):
        va, vb = a.get(k), b.get(k)
        if isinstance(va, dict) and isinstance(vb, dict):
            diffs.extend(_diff_coverage(va, vb, prefix + k + "."))
        elif va != vb:
            diffs.append(prefix + k)
    return diffs


class InformationCache:
    def __init__(self, store: Store, access_provider, clock, ttl_days: int = 7):
        self.store = store
        self.access = access_provider
        self.clock = clock
        self.ttl_days = ttl_days

    # ---- publication ------------------------------------------------------
    def publish(self, *, execution_id: str, rec: IntentRecord, recipe: dict[str, Any], cut, access, rows: list[dict[str, Any]],
                validation_passed: bool, complete: bool, relation_ref: str, receipt_ref: str) -> dict[str, Any] | None:
        if not (validation_passed and complete):
            self._event(None, rec.request_id, "publication_refused", {"reason": "partial or failed results are never cached"})
            return None
        now = self.clock.now()
        is_id = "is_" + uuid.uuid4().hex[:10]
        dims = list(rec.intent.dimensions)
        clean = [{**{d: r[d] for d in dims}, **{k: int(r[k] or 0) for k in COUNT_FIELDS},
                  "conversion_rate": rate(int(r["converted"] or 0), int(r["eligible_mature"] or 0))} for r in rows]
        latest = cut.cut_mode != "pinned"
        manifest = {
            "information_set_id": is_id, "semantic_fingerprint": semantic_hash(rec), "coverage_key": coverage_key(rec),
            "coverage_payload": coverage_payload(rec), "semantic_contract_ref": f"contracts/{rec.request_id}/v{rec.version}.json",
            "recipe_versions": rec.intent.resolved_recipes, "cut_mode": cut.cut_mode, "source_cut_fingerprint": cut.fingerprint,
            "source_cut_vector": cut.to_dict()["entries"], "access_fingerprint": access.fingerprint,
            "access_reuse_mode": access.reuse_mode, "access_class": access.access_class,
            "access_class_certificate_ref": access.class_certificate_ref, "policy_version": access.policy_version,
            "coverage": {"state": rec.intent.scope.get("state"), "lob": rec.intent.scope.get("lob"),
                         "population": rec.intent.population, "cohort": rec.intent.cohort.model_dump() if rec.intent.cohort else None,
                         "window": rec.intent.window.model_dump() if rec.intent.window else None,
                         "business_timezone": rec.intent.business_timezone, "maturity_policy": rec.intent.maturity_policy,
                         "knowledge_cutoff": rec.intent.knowledge_cutoff, "dimensions": dims, "output_grain": dims},
            "metric_state": {k: "count" for k in COUNT_FIELDS} | {"conversion_rate": "sum_converted_over_sum_eligible_mature"},
            "complete": True, "exactness": "exact_for_confirmed_mature_population", "validation": "passed",
            "producer_execution_id": execution_id, "producer_request_id": rec.request_id, "producer_receipt_ref": receipt_ref,
            "relation_ref": relation_ref, "created_at": to_utc_z(now), "replayable_until": cut.replayable_until,
            "freshness_valid_until": to_utc_z(now + timedelta(hours=1)) if latest else None,
            "expires_at": to_utc_z(now + timedelta(days=self.ttl_days)),
            "result_digest": fingerprint(clean),
        }
        with self.store.tx() as con:
            con.execute("INSERT INTO cache_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                is_id, manifest["coverage_key"], manifest["semantic_fingerprint"], dumps(dims), cut.fingerprint,
                access.reuse_key, access.reuse_mode, cut.cut_mode, recipe["id"], recipe["version"], dumps(manifest), relation_ref,
                dumps(clean), manifest["created_at"], manifest["expires_at"], manifest["freshness_valid_until"], "valid",
                "available", access.policy_version))
        self._event(is_id, rec.request_id, "published", {"execution_id": execution_id, "digest": manifest["result_digest"]})
        return manifest

    # ---- lookup -------------------------------------------------------------
    def lookup(self, rec: IntentRecord, recipe: dict[str, Any], cut, access, requester, port_refs: list[str]) -> CacheDecision:
        now = self.clock.now_z()
        key = coverage_key(rec)
        dims = list(rec.intent.dimensions)
        decision = CacheDecision(outcome="miss")
        cands = self.store.q("SELECT * FROM cache_entries WHERE recipe_id=? ORDER BY created_at DESC", (recipe["id"],))
        best = None
        for e in cands:
            m = loads(e["manifest_json"])
            reason = None
            if e["recipe_version"] != recipe["version"]:
                reason = f"recipe version {e['recipe_version']} differs"
            elif e["coverage_key"] != key:
                reason = "coverage differs: " + ", ".join(_diff_coverage(m["coverage_payload"], coverage_payload(rec))[:4])
            elif e["state"] != "valid":
                reason = f"entry {e['state']}"
            elif e["expires_at"] <= now:
                reason = "storage expiry passed"
            elif e["cut_mode"] != cut.cut_mode:
                reason = f"cut mode {e['cut_mode']} vs {cut.cut_mode}"
            elif e["source_cut_fingerprint"] != cut.fingerprint:
                reason = "different source cut (publication vector)"
            elif e["cut_mode"] != "pinned" and (e["freshness_valid_until"] or "") <= now:
                reason = "latest-mode freshness deadline passed"
            elif e["policy_version"] != access.policy_version:
                reason = "policy dependency version changed"
            elif e["access_reuse_key"] != access.reuse_key:
                reason = "different effective-access identity (no certified shared class)"
            else:
                cached_dims = loads(e["dimensions_json"])
                if dims == cached_dims or (set(dims) == set(cached_dims) and len(dims) == len(cached_dims)):
                    best = ("exact", e, m)
                    break
                if set(dims) < set(cached_dims):
                    best = best or ("derived", e, m)
                    continue
                reason = f"requires dimension(s) {sorted(set(dims) - set(cached_dims))} not in cached set"
            decision.rejected.append({"information_set_id": e["information_set_id"], "reason": reason})
        if best is None:
            return decision
        kind, e, m = best
        ok, why = self.access.reauthorize(requester, port_refs, m)
        decision.reauthorization = why
        if not ok:
            decision.rejected.append({"information_set_id": e["information_set_id"], "reason": f"re-authorization failed: {why}"})
            return decision
        rows = loads(e["rows_json"])
        decision.outcome = kind
        decision.entry = {**e, "manifest": m}
        decision.rows = rollup(rows, dims) if kind == "derived" else rows
        decision.rule = "rw.derived_rollup_sum_v1" if kind == "derived" else "exact"
        decision.availability = ("retained_result_only; source recomputation unavailable"
                                 if m.get("replayable_until") and m["replayable_until"] < now else "recomputable")
        self._event(e["information_set_id"], rec.request_id, f"hit_{kind}", {"rule": decision.rule})
        return decision

    # ---- invalidation -----------------------------------------------------
    def invalidate_policy_change(self, new_policy_version: str) -> int:
        n = self.store.x("UPDATE cache_entries SET state='invalidated' WHERE policy_version<>? AND state='valid'", (new_policy_version,))
        self._event(None, None, "invalidated_policy_change", {"count": n, "policy_version": new_policy_version})
        return n

    def invalidate_latest_on_publication(self) -> int:
        return self.store.x("UPDATE cache_entries SET state='invalidated' WHERE cut_mode<>'pinned' AND state='valid'")

    def evict_expired(self) -> int:
        now = self.clock.now_z()
        # artifact eviction keeps the manifest and receipt digest; availability is recorded separately
        return self.store.x("UPDATE cache_entries SET state='evicted', availability='evicted', rows_json='[]' "
                            "WHERE expires_at<=? AND state='valid'", (now,))

    def entries(self) -> list[dict[str, Any]]:
        return self.store.q("SELECT information_set_id, recipe_id, recipe_version, cut_mode, access_reuse_mode, dimensions_json, "
                            "created_at, expires_at, state, availability FROM cache_entries ORDER BY created_at DESC")

    def _event(self, is_id, request_id, event, detail) -> None:
        self.store.x("INSERT INTO cache_events (information_set_id, request_id, event, detail_json, at) VALUES (?,?,?,?,?)",
                     (is_id, request_id, event, dumps(detail), self.clock.now_z()))
