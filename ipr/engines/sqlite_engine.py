"""Local SQLite data-plane engine.

Simulates the governed execution-engine behaviour the control plane relies on:

* requester-scoped sessions from a bounded pool isolated by principal and
  purpose (ADR-01); sessions are serialized and their settings restored;
* product ports are expanded by the product-owned snapshot adapter
  (publication sequence) and a *simulated native* row-access policy evaluated
  at query time against the current mapping table — the compiler never
  injects authorization predicates itself;
* asynchronous submission with query IDs, QUERY_TAG, persisted query history
  searchable by session/tag with a result limit, cancellation via interrupt;
* completed-query statistics (rows, elapsed); spill is reported unavailable;
* fault injection for lost acknowledgements, failures and delays (tests).
"""

from __future__ import annotations

import itertools
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..registry import Registry
from .base import (
    PORT_RE,
    EngineError,
    QueryRecord,
    SubmissionAckLost,
    sql_hash,
    udf_add_local_days,
    udf_day_start_plus,
    udf_hash_mod,
    udf_local_date,
    udf_utc_date,
    validate_relation_name,
)

HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS engine_query_history (
  query_id TEXT PRIMARY KEY, session_id TEXT, principal TEXT, query_tag TEXT, kind TEXT, sql_hash TEXT,
  status TEXT, started_at TEXT, ended_at TEXT, rows_produced INTEGER, elapsed_ms REAL, error TEXT, output_relation TEXT);
CREATE INDEX IF NOT EXISTS ix_eqh_tag ON engine_query_history(query_tag);
"""


def _matches(rule, tag: str) -> bool:
    """A fault rule is a substring or a tuple of substrings that must all occur."""
    parts = rule if isinstance(rule, (tuple, list)) else (rule,)
    return all(p in tag for p in parts)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    con.create_function("ipr_local_date", 2, udf_local_date, deterministic=True)
    con.create_function("ipr_add_local_days", 3, udf_add_local_days, deterministic=True)
    con.create_function("ipr_utc_date", 1, udf_utc_date, deterministic=True)
    con.create_function("ipr_day_start_plus", 2, udf_day_start_plus, deterministic=True)
    con.create_function("ipr_hash_mod", 2, udf_hash_mod, deterministic=True)
    return con


class SQLiteSession:
    _ids = itertools.count(1)

    def __init__(self, engine: "SQLiteEngine", principal: str, purpose: str):
        self.engine = engine
        self.principal = principal
        self.purpose = purpose
        self.session_id = f"sess_{next(self._ids):05d}_{uuid.uuid4().hex[:6]}"
        self._con = _connect(engine.path)
        self._lock = threading.Lock()
        self.query_tag = ""
        self._running: dict[str, threading.Thread] = {}
        self._cancelled: set[str] = set()

    # -- session settings ------------------------------------------------
    def set_query_tag(self, tag: str) -> None:
        self.query_tag = tag

    def reset(self) -> None:
        self.query_tag = ""

    # -- port expansion (product snapshot adapter + native policy) --------
    def _expand(self, sql: str, cut: dict[str, int]) -> tuple[str, dict[str, Any]]:
        extra: dict[str, Any] = {"__subject": self.principal, "__purpose": self.purpose}

        def repl(m):
            ref = f"{m.group(1)}/{m.group(2)}"
            if ref not in cut:
                raise EngineError(f"port {ref} has no bound publication in the source cut")
            port = self.engine.registry.port(ref)
            pname = f"__seq_{m.group(1)}__{m.group(2)}"
            extra[pname] = int(cut[ref])
            cols = ", ".join(port["columns"].keys())
            base = port["base_relation"]
            # product snapshot adapter: rows first published at or before the seq
            snapshot = f"first_pub_seq <= :{pname}"
            # simulated native row access policy, evaluated at query time
            policy = ("state IN (SELECT state FROM policy_state_mapping "
                      "WHERE subject = :__subject AND purpose = :__purpose)")
            return f"(SELECT {cols} FROM {base} WHERE {snapshot} AND {policy})"

        return PORT_RE.sub(repl, sql), extra

    # -- synchronous metadata/probe reads ---------------------------------
    def fetch_all(self, sql: str, params: dict[str, Any] | None = None, *, cut: dict[str, int] | None = None) -> list[dict[str, Any]]:
        expanded, extra = self._expand(sql, cut or {})
        with self._lock:
            cur = self._con.execute(expanded, {**(params or {}), **extra})
            return [dict(r) for r in cur.fetchall()]

    def explain(self, sql: str, params: dict[str, Any] | None = None, *, cut: dict[str, int] | None = None) -> list[str]:
        expanded, extra = self._expand(sql, cut or {})
        with self._lock:
            rows = self._con.execute("EXPLAIN QUERY PLAN " + expanded, {**(params or {}), **extra}).fetchall()
        return [r["detail"] for r in rows]

    # -- asynchronous submission -----------------------------------------
    def submit_async(self, sql: str, params: dict[str, Any], *, kind: str, cut: dict[str, int],
                     output_relation: str | None = None) -> str:
        if self.engine.token_expired(self.principal):
            from .base import TokenExpired
            raise TokenExpired(f"delegated token for {self.principal} expired")
        if output_relation:
            validate_relation_name(output_relation)
        expanded, extra = self._expand(sql, cut)
        query_id = f"q_{uuid.uuid4().hex[:16]}"
        rec = QueryRecord(query_id=query_id, session_id=self.session_id, principal=self.principal,
                          query_tag=self.query_tag, kind=kind, sql_hash=sql_hash(sql, params), status="RUNNING",
                          started_at=_now(), output_relation=output_relation)
        self.engine._record(rec)
        tag = self.query_tag
        faults = self.engine.faults

        def run():
            t0 = time.perf_counter()
            try:
                delay = 0.0
                for frag, secs in faults.get("delay", {}).items():
                    if frag in tag:
                        delay = max(delay, secs)
                if delay:
                    time.sleep(delay)
                if query_id in self._cancelled:
                    raise sqlite3.OperationalError("interrupted")
                for frag in faults.get("fail", []):
                    if _matches(frag, tag):
                        raise EngineError(f"injected failure for {frag}")
                with self._lock:
                    cur = self._con.execute(expanded, {**params, **extra})
                    rows = None
                    if output_relation:
                        rows = self._con.execute(f"SELECT COUNT(*) FROM {output_relation}").fetchone()[0]
                    else:
                        result = [dict(r) for r in cur.fetchall()]
                        self.engine._results[query_id] = result
                        rows = len(result)
                rec.status = "SUCCESS"
                rec.rows_produced = rows
            except sqlite3.OperationalError as exc:
                rec.status = "CANCELLED" if "interrupt" in str(exc).lower() else "FAILED"
                rec.error = str(exc)
            except Exception as exc:  # noqa: BLE001 - reported to the runtime
                rec.status = "FAILED"
                rec.error = f"{type(exc).__name__}: {exc}"
            finally:
                rec.elapsed_ms = (time.perf_counter() - t0) * 1000
                rec.ended_at = _now()
                self.engine._record(rec)

        th = threading.Thread(target=run, name=f"q-{query_id}", daemon=True)
        self._running[query_id] = th
        th.start()
        for frag in list(faults.get("lose_ack", [])):
            if frag in tag:
                faults["lose_ack"].remove(frag)  # one-shot
                raise SubmissionAckLost(f"acknowledgement lost for tag {tag}")
        return query_id

    def status(self, query_id: str) -> QueryRecord:
        return self.engine.query_record(query_id)

    def cancel(self, query_id: str) -> None:
        rec = self.engine.query_record(query_id)
        if rec.status == "RUNNING":
            self._cancelled.add(query_id)
            self._con.interrupt()

    def wait(self, query_id: str, timeout: float = 600) -> QueryRecord:
        th = self._running.get(query_id)
        if th:
            th.join(timeout)
        return self.status(query_id)

    def close(self) -> None:
        try:
            self._con.close()
        except sqlite3.Error:
            pass


class SQLiteEngine:
    engine_name = "sqlite"
    dialect = "sqlite"

    def __init__(self, path: Path, registry: Registry, pool_limit_per_principal: int = 8):
        self.path = Path(path)
        self.registry = registry
        self.pool_limit = pool_limit_per_principal
        self._pool: dict[tuple[str, str], list[SQLiteSession]] = {}
        self._in_use: dict[tuple[str, str], int] = {}
        self._pool_lock = threading.Condition()
        self._hist_con = _connect(self.path)
        self._hist_lock = threading.Lock()
        with self._hist_lock:
            self._hist_con.executescript(HISTORY_DDL)
        self._records: dict[str, QueryRecord] = {}
        self._results: dict[str, list[dict[str, Any]]] = {}
        self.faults: dict[str, Any] = {"lose_ack": [], "fail": [], "delay": {}}
        self._expired_tokens: set[str] = set()

    # -- delegated identity ----------------------------------------------
    def expire_token(self, principal: str) -> None:
        self._expired_tokens.add(principal)

    def renew_token(self, principal: str) -> None:
        self._expired_tokens.discard(principal)

    def token_expired(self, principal: str) -> bool:
        return principal in self._expired_tokens

    # -- pool ----------------------------------------------------------------
    def acquire(self, principal: str, purpose: str, timeout: float = 60) -> SQLiteSession:
        key = (principal, purpose)
        deadline = time.monotonic() + timeout
        with self._pool_lock:
            while True:
                idle = self._pool.setdefault(key, [])
                if idle:
                    sess = idle.pop()
                    break
                if self._in_use.get(key, 0) < self.pool_limit:
                    sess = SQLiteSession(self, principal, purpose)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EngineError(f"no session available for {principal}")
                self._pool_lock.wait(remaining)
            self._in_use[key] = self._in_use.get(key, 0) + 1
        return sess

    def release(self, sess: SQLiteSession) -> None:
        sess.reset()
        key = (sess.principal, sess.purpose)
        with self._pool_lock:
            self._in_use[key] = max(0, self._in_use.get(key, 1) - 1)
            self._pool.setdefault(key, []).append(sess)
            self._pool_lock.notify_all()

    # -- history -------------------------------------------------------------
    def _record(self, rec: QueryRecord) -> None:
        self._records[rec.query_id] = rec
        with self._hist_lock:
            self._hist_con.execute(
                "INSERT OR REPLACE INTO engine_query_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec.query_id, rec.session_id, rec.principal, rec.query_tag, rec.kind, rec.sql_hash, rec.status,
                 rec.started_at, rec.ended_at, rec.rows_produced, rec.elapsed_ms, rec.error, rec.output_relation))

    def query_record(self, query_id: str) -> QueryRecord:
        if query_id in self._records:
            return self._records[query_id]
        with self._hist_lock:
            row = self._hist_con.execute("SELECT * FROM engine_query_history WHERE query_id=?", (query_id,)).fetchone()
        if not row:
            raise EngineError(f"unknown query id {query_id}")
        return QueryRecord(**{k: row[k] for k in row.keys()})

    def query_history_by_tag(self, principal: str, tag: str, result_limit: int = 100,
                             include_running: bool = True) -> tuple[list[QueryRecord], bool]:
        """Search history like QUERY_HISTORY_BY_SESSION: the result limit applies
        *before* the tag filter, so the caller must check completeness."""
        with self._hist_lock:
            rows = self._hist_con.execute(
                "SELECT * FROM engine_query_history WHERE principal=? ORDER BY started_at DESC LIMIT ?",
                (principal, result_limit)).fetchall()
            total = self._hist_con.execute("SELECT COUNT(*) FROM engine_query_history WHERE principal=?", (principal,)).fetchone()[0]
        complete = total <= result_limit
        recs = [QueryRecord(**{k: r[k] for k in r.keys()}) for r in rows if r["query_tag"] == tag]
        if not include_running:
            recs = [r for r in recs if r.status != "RUNNING"]
        return recs, complete

    def result(self, query_id: str) -> list[dict[str, Any]] | None:
        return self._results.get(query_id)

    def operator_stats(self, query_id: str) -> dict[str, Any]:
        rec = self.query_record(query_id)
        if rec.status == "RUNNING":
            return {"state": "unavailable", "reason": "query not complete; operator statistics exist only for completed queries"}
        return {"state": "collected", "rows_produced": rec.rows_produced, "elapsed_ms": rec.elapsed_ms,
                "spill_local_bytes": None, "spill_remote_bytes": None,
                "spill_reason": "not observable in the local SQLite engine"}

    # -- relations -----------------------------------------------------------
    def relation_exists(self, name: str) -> bool:
        validate_relation_name(name)
        with self._hist_lock:
            return self._hist_con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def drop_relation(self, name: str) -> None:
        validate_relation_name(name)
        with self._hist_lock:
            self._hist_con.execute(f"DROP TABLE IF EXISTS {name}")

    def rename_relation(self, old: str, new: str) -> None:
        validate_relation_name(old)
        validate_relation_name(new)
        with self._hist_lock:
            self._hist_con.execute(f"ALTER TABLE {old} RENAME TO {new}")

    def read_relation(self, name: str, limit: int = 10000) -> list[dict[str, Any]]:
        validate_relation_name(name)
        with self._hist_lock:
            cur = self._hist_con.execute(f"SELECT * FROM {name} LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]

    def admin_execute(self, sql: str, params: tuple = ()) -> None:
        """Administrative statements on fixture metadata (publications, policy mapping)."""
        with self._hist_lock:
            self._hist_con.execute(sql, params)

    def admin_query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._hist_lock:
            return [dict(r) for r in self._hist_con.execute(sql, params).fetchall()]

    def close(self) -> None:
        with self._pool_lock:
            for sessions in self._pool.values():
                for s in sessions:
                    s.close()
            self._pool.clear()
