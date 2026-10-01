"""Dialect-aware SQL fragments for the approved operators.

Parameter values are always connector binds (``:name``). Object names come
only from port placeholders ``{{port:product/port}}`` (expanded by the engine's
product snapshot adapter) and upstream relation placeholders ``{{rel:node}}``
(resolved by the runtime to allowlisted, attempt-specific stage names).
"""

from __future__ import annotations

from dataclasses import dataclass

QUOTE_PORT = "{{port:quote_fdp/quoted_opportunities_v2}}"
BIND_PORT = "{{port:policy_fdp/known_bind_events_v2}}"

SQLITE_FUNCTIONS = {"ipr_local_date", "ipr_add_local_days", "ipr_utc_date", "ipr_day_start_plus", "ipr_hash_mod"}


@dataclass(frozen=True)
class Dialect:
    name: str

    def local_date(self, expr: str) -> str:
        if self.name == "snowflake":
            return f"TO_VARCHAR(TO_DATE(CONVERT_TIMEZONE('UTC', :tz, {expr})))"
        return f"ipr_local_date({expr}, :tz)"

    def add_local_days(self, expr: str) -> str:
        if self.name == "snowflake":
            return f"CONVERT_TIMEZONE(:tz, 'UTC', DATEADD(day, :window_days, CONVERT_TIMEZONE('UTC', :tz, {expr})))"
        return f"ipr_add_local_days({expr}, :tz, :window_days)"

    def utc_date(self, expr: str) -> str:
        if self.name == "snowflake":
            return f"TO_VARCHAR(TO_DATE({expr}))"
        return f"ipr_utc_date({expr})"

    def day_start_plus(self, date_expr: str, days_expr: str) -> str:
        if self.name == "snowflake":
            return f"DATEADD(day, {days_expr}, TO_TIMESTAMP_NTZ({date_expr}))"
        return f"ipr_day_start_plus({date_expr}, {days_expr})"

    def hash_mod(self, expr: str, n_expr: str) -> str:
        if self.name == "snowflake":
            # Snowflake HASH() is not a documented cross-version-stable hash; a
            # deployment must pin an approved stable hash UDF (hash version).
            return f"MOD(ABS(HASH(TRIM({expr}))), {n_expr})"
        return f"ipr_hash_mod({expr}, {n_expr})"


def knowledge_filter(alias: str) -> str:
    return (f"{alias}.knowledge_at <= :cutoff AND ({alias}.superseded_knowledge_at IS NULL "
            f"OR {alias}.superseded_knowledge_at > :cutoff)")


def cohort_sql(d: Dialect, identity: str, partitioned: bool) -> str:
    """canonical_event_selection + maturity/quality classification at opportunity grain."""
    part = f", {d.hash_mod('opportunity_id', ':n_partitions')} AS partition_id" if partitioned else ""
    if identity == "submission":
        return f"""
WITH branches AS (
  SELECT q.quote_branch_id, q.submission_opportunity_id AS opportunity_id, q.job_version, q.product_version,
         q.channel, q.quality_flag, q.quote_generated_at, q.knowledge_at
  FROM {QUOTE_PORT} q
  WHERE {knowledge_filter('q')}
    AND q.quote_status = 'quoted' AND q.state = :state AND q.lob = :lob
), ranked AS (
  SELECT b.*, ROW_NUMBER() OVER (PARTITION BY b.opportunity_id
                                 ORDER BY b.quote_generated_at, b.job_version, b.quote_branch_id) AS rn
  FROM branches b
), firsts AS (
  SELECT r.opportunity_id, r.quote_branch_id AS first_branch_id, r.product_version, r.channel, r.quality_flag,
         r.quote_generated_at AS anchor_at, r.quote_generated_at AS window_start_at,
         {d.add_local_days('r.quote_generated_at')} AS window_end_at, r.knowledge_at
  FROM ranked r
  WHERE r.rn = 1
    AND {d.local_date('r.quote_generated_at')} >= :cohort_start
    AND {d.local_date('r.quote_generated_at')} < :cohort_end
)
SELECT opportunity_id, first_branch_id, product_version, channel, anchor_at, window_start_at, window_end_at,
       knowledge_at AS max_knowledge_at,
       CASE WHEN quality_flag <> 'ok' THEN 'quality_excluded'
            WHEN window_end_at > :cutoff THEN 'immature'
            ELSE 'mature' END AS cohort_class{part}
FROM firsts"""
    # legacy per-branch identity, UTC dates, inclusive day window
    qdate = d.utc_date("q.quote_generated_at")
    return f"""
WITH firsts AS (
  SELECT q.quote_branch_id AS opportunity_id, q.quote_branch_id AS first_branch_id, q.product_version, q.channel,
         q.quality_flag, q.quote_generated_at AS anchor_at,
         {d.day_start_plus(qdate, '0')} AS window_start_at,
         {d.day_start_plus(qdate, ':window_days + 1')} AS window_end_at, q.knowledge_at
  FROM {QUOTE_PORT} q
  WHERE {knowledge_filter('q')}
    AND q.quote_status = 'quoted' AND q.state = :state AND q.lob = :lob
    AND {qdate} >= :cohort_start AND {qdate} < :cohort_end
)
SELECT opportunity_id, first_branch_id, product_version, channel, anchor_at, window_start_at, window_end_at,
       knowledge_at AS max_knowledge_at,
       CASE WHEN quality_flag <> 'ok' THEN 'quality_excluded'
            WHEN window_end_at > :cutoff THEN 'immature'
            ELSE 'mature' END AS cohort_class{part}
FROM firsts"""


def linked_binds_sql(d: Dialect, identity: str, cohort_ref: str, partitioned: bool) -> str:
    """key_reduction: committed binds known by the cutoff, resolved to the canonical
    opportunity key and restricted (semijoin) to mature cohort keys."""
    part = f", {d.hash_mod('l.opportunity_id', ':n_partitions')} AS partition_id" if partitioned else ""
    if identity == "submission":
        linkage = f"""(SELECT q.quote_branch_id, q.submission_opportunity_id AS opportunity_id
   FROM {QUOTE_PORT} q WHERE {knowledge_filter('q')})"""
    else:
        linkage = f"""(SELECT q.quote_branch_id, q.quote_branch_id AS opportunity_id
   FROM {QUOTE_PORT} q WHERE {knowledge_filter('q')})"""
    return f"""
SELECT b.bind_event_id, l.opportunity_id, b.bound_at, b.knowledge_at{part}
FROM {BIND_PORT} b
JOIN {linkage} l ON l.quote_branch_id = b.quote_branch_id
WHERE b.bind_status = 'committed' AND {knowledge_filter('b')}
  AND l.opportunity_id IN (SELECT c.opportunity_id FROM {cohort_ref} c WHERE c.cohort_class = 'mature')"""


def outcome_sql(cohort_ref: str, binds_ref: str, part_filter_c: str = "", part_filter_k: str = "") -> str:
    """window_exists: one outcome per opportunity; unmatched cohort rows preserved."""
    k_where = f"k.opportunity_id = c.opportunity_id AND k.bound_at >= c.window_start_at AND k.bound_at < c.window_end_at{part_filter_k}"
    return f"""
SELECT c.opportunity_id, c.product_version, c.channel, c.cohort_class, c.window_start_at, c.window_end_at,
       c.max_knowledge_at,
       CASE WHEN c.cohort_class = 'mature' AND EXISTS (SELECT 1 FROM {binds_ref} k WHERE {k_where}) THEN 1 ELSE 0 END AS converted,
       CASE WHEN c.cohort_class = 'mature' THEN (SELECT MIN(k.bound_at) FROM {binds_ref} k WHERE {k_where}) END AS first_bind_at,
       CASE WHEN c.cohort_class = 'mature' THEN (SELECT MAX(k.knowledge_at) FROM {binds_ref} k WHERE {k_where}) END AS max_bind_knowledge_at
FROM {cohort_ref} c
WHERE 1 = 1{part_filter_c}"""


def aggregate_select(dims: list[str], source: str) -> str:
    dim_cols = "".join(f"{x}, " for x in dims)
    group = f"\nGROUP BY {', '.join(dims)}" if dims else ""
    return f"""
SELECT {dim_cols}COUNT(*) AS eligible_all,
       COALESCE(SUM(CASE WHEN cohort_class = 'mature' THEN 1 ELSE 0 END), 0) AS eligible_mature,
       COALESCE(SUM(CASE WHEN cohort_class = 'immature' THEN 1 ELSE 0 END), 0) AS immature,
       COALESCE(SUM(CASE WHEN cohort_class = 'quality_excluded' THEN 1 ELSE 0 END), 0) AS quality_excluded,
       COALESCE(SUM(converted), 0) AS converted,
       COUNT(*) - COUNT(DISTINCT opportunity_id) AS grain_violations,
       (SELECT COUNT(*) - COUNT(DISTINCT g.opportunity_id) FROM {source} g) AS outcome_grain_violations,
       COALESCE(SUM(CASE WHEN converted = 1 AND NOT (first_bind_at >= window_start_at AND first_bind_at < window_end_at)
                         THEN 1 ELSE 0 END), 0) AS window_violations,
       MAX(max_knowledge_at) AS max_knowledge_at,
       MAX(max_bind_knowledge_at) AS max_bind_knowledge_at
FROM {source}{group}"""


def merge_sql(dims: list[str], refs: list[str]) -> str:
    union = "\n  UNION ALL ".join(f"SELECT * FROM {r}" for r in refs)
    dim_cols = "".join(f"{x}, " for x in dims)
    group = f"\nGROUP BY {', '.join(dims)}" if dims else ""
    return f"""
SELECT {dim_cols}SUM(eligible_all) AS eligible_all, SUM(eligible_mature) AS eligible_mature, SUM(immature) AS immature,
       SUM(quality_excluded) AS quality_excluded, SUM(converted) AS converted, SUM(grain_violations) AS grain_violations,
       MAX(outcome_grain_violations) AS outcome_grain_violations, SUM(window_violations) AS window_violations, MAX(max_knowledge_at) AS max_knowledge_at,
       MAX(max_bind_knowledge_at) AS max_bind_knowledge_at
FROM (
  {union}
) u{group}"""
