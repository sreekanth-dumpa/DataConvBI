"""Control-plane store (local SQLite stand-in for the Aurora domain store).

Holds intents, confirmations, resolutions, obligation manifests, the persistent
plan registry (logical plans, physical versions, applicability, certificates),
execution instances, node state, node attempts, adaptation versions, the
transactional outbox, result manifests, cache entries, profiles, receipts and
the hash-linked trace. ``claim`` uses ``BEGIN IMMEDIATE`` as the local
analogue of ``FOR UPDATE SKIP LOCKED`` claiming, with leases and fencing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, conversation_id TEXT, parent_request_id TEXT,
  subject TEXT, raw_text TEXT, created_at TEXT, interpretation_receipt TEXT);
CREATE TABLE IF NOT EXISTS intents (request_id TEXT, version INTEGER, intent_json TEXT, semantic_hash TEXT, status TEXT,
  created_at TEXT, PRIMARY KEY (request_id, version));
CREATE TABLE IF NOT EXISTS confirmations (request_id TEXT, semantic_hash TEXT, actor TEXT, at TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS resolutions (request_id TEXT, version INTEGER, resolution_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS obligation_manifests (manifest_id TEXT PRIMARY KEY, request_id TEXT, pass TEXT, manifest_json TEXT,
  frozen_hash TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS logical_plans (plan_id TEXT, version INTEGER, semantic_fingerprint TEXT, coverage_key TEXT,
  logical_fingerprint TEXT, recipe_id TEXT, recipe_version TEXT, plan_json TEXT, created_at TEXT, PRIMARY KEY (plan_id, version));
CREATE TABLE IF NOT EXISTS physical_plans (physical_id TEXT PRIMARY KEY, plan_id TEXT, logical_version INTEGER, variant TEXT,
  physical_fingerprint TEXT, plan_json TEXT, certificate_json TEXT, parent_physical_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS plan_applicability (plan_id TEXT PRIMARY KEY, applicability_json TEXT, certified INTEGER, updated_at TEXT);
CREATE TABLE IF NOT EXISTS executions (execution_id TEXT PRIMARY KEY, request_id TEXT, plan_id TEXT, physical_id TEXT,
  workflow_id TEXT UNIQUE, state TEXT, principal TEXT, purpose TEXT, tenant TEXT, priority REAL, cut_json TEXT, access_json TEXT,
  budget_json TEXT, estimate_json TEXT, admitted_at TEXT, started_at TEXT, finished_at TEXT, error TEXT, plan_version INTEGER,
  revisions INTEGER DEFAULT 0, cancel_requested INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS nodes (execution_id TEXT, node_id TEXT, spec_json TEXT, state TEXT, depends_json TEXT, priority REAL,
  plan_version INTEGER, lease_owner TEXT, lease_expires_at REAL, fence INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
  output_relation TEXT, observations_json TEXT, share_key TEXT, shared_from TEXT, updated_at TEXT,
  PRIMARY KEY (execution_id, node_id));
CREATE INDEX IF NOT EXISTS ix_nodes_state ON nodes(state);
CREATE TABLE IF NOT EXISTS node_attempts (attempt_id TEXT PRIMARY KEY, execution_id TEXT, node_id TEXT, attempt_no INTEGER,
  fence INTEGER, query_tag TEXT, query_id TEXT, sql_hash TEXT, params_hash TEXT, output_relation TEXT, session_id TEXT,
  workflow_id TEXT, activity_id TEXT, state TEXT, submitted_at TEXT, ended_at TEXT, metrics_json TEXT, error TEXT,
  reconciliation_json TEXT);
CREATE TABLE IF NOT EXISTS adaptations (execution_id TEXT, version INTEGER, prev_version INTEGER, adaptation_json TEXT,
  created_at TEXT, PRIMARY KEY (execution_id, version));
CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, payload TEXT, state TEXT, created_at TEXT,
  claimed_by TEXT, claimed_until REAL);
CREATE TABLE IF NOT EXISTS result_manifests (execution_id TEXT, output_key TEXT UNIQUE, manifest_json TEXT, digest TEXT,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS cache_entries (information_set_id TEXT PRIMARY KEY, coverage_key TEXT, semantic_fingerprint TEXT,
  dimensions_json TEXT, source_cut_fingerprint TEXT, access_reuse_key TEXT, access_reuse_mode TEXT, cut_mode TEXT,
  recipe_id TEXT, recipe_version TEXT, manifest_json TEXT, relation_ref TEXT, rows_json TEXT, created_at TEXT, expires_at TEXT,
  freshness_valid_until TEXT, state TEXT, availability TEXT, policy_version TEXT);
CREATE TABLE IF NOT EXISTS cache_events (id INTEGER PRIMARY KEY AUTOINCREMENT, information_set_id TEXT, request_id TEXT,
  event TEXT, detail_json TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS profiles (profile_key TEXT PRIMARY KEY, port_scope TEXT, cut_fingerprint TEXT, scope_hash TEXT,
  stat_name TEXT, value_json TEXT, exact INTEGER, collected_at TEXT, access_reuse_key TEXT);
CREATE TABLE IF NOT EXISTS receipts (execution_id TEXT PRIMARY KEY, receipt_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS stages (relation TEXT PRIMARY KEY, execution_id TEXT, node_id TEXT, attempt_id TEXT,
  owner_principal TEXT, created_at TEXT, expires_at TEXT, state TEXT, policy TEXT);
CREATE TABLE IF NOT EXISTS trace_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, trace_id TEXT, span_id TEXT, parent_span_id TEXT,
  request_id TEXT, execution_id TEXT, stage TEXT, event_json TEXT, prev_hash TEXT, hash TEXT, at TEXT);
CREATE INDEX IF NOT EXISTS ix_trace_req ON trace_events(request_id);
CREATE TABLE IF NOT EXISTS telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT, node_id TEXT, attempt_id TEXT,
  query_id TEXT, metric TEXT, value REAL, state TEXT, reason TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS assessments (assessment_id TEXT PRIMARY KEY, execution_id TEXT, request_id TEXT, doc_path TEXT,
  case_json TEXT, created_at TEXT);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        self._con().executescript(SCHEMA)

    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=30000")
            con.row_factory = sqlite3.Row
            self._local.con = con
        return con

    @contextmanager
    def tx(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        con = self._con()
        con.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield con
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise

    def q(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self._con().execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        r = self._con().execute(sql, params).fetchone()
        return dict(r) if r else None

    def x(self, sql: str, params: tuple | dict = ()) -> int:
        cur = self._con().execute(sql, params)
        return cur.rowcount


def dumps(v: Any) -> str:
    from .canonical import stable_json
    return stable_json(v)


def loads(s: str | None) -> Any:
    return None if s is None else json.loads(s)
