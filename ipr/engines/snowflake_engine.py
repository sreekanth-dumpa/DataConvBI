"""Optional live Snowflake adapter (not integration-tested in this POC).

Implements the same session contract as the local engine using the Python
connector: requester-scoped connections (External OAuth token per subject —
never a broader service principal), ``QUERY_TAG`` set on the isolated session
before ``execute_async``, ``get_query_status`` polling, cancellation via
``SYSTEM$CANCEL_QUERY``, and tag-based reconciliation through
``INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION`` with an explicit
``RESULT_LIMIT`` and completeness flag.

Product ports are expanded to product-owned snapshot views supplied in
``port_views`` (e.g. a governed view that accepts a publication id), never to
base tables with Time Travel. Native row-access and masking policies apply in
the engine; nothing here duplicates them.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .base import PORT_RE, EngineError, QueryRecord, SubmissionAckLost, TokenExpired, sql_hash, validate_relation_name


class SnowflakeSession:
    def __init__(self, engine: "SnowflakeEngine", principal: str, purpose: str):
        self.engine = engine
        self.principal = principal
        self.purpose = purpose
        token = engine.token_provider(principal)
        if token is None:
            raise TokenExpired(f"no delegated token for {principal}")
        import snowflake.connector  # optional dependency

        self._con = snowflake.connector.connect(authenticator="oauth", token=token, **engine.connect_args)
        self.session_id = str(self._con.session_id)
        self._lock = threading.Lock()
        self.query_tag = ""

    def set_query_tag(self, tag: str) -> None:
        with self._lock:
            self._con.cursor().execute("ALTER SESSION SET QUERY_TAG = %(t)s", {"t": tag})
        self.query_tag = tag

    def reset(self) -> None:
        with self._lock:
            self._con.cursor().execute("ALTER SESSION UNSET QUERY_TAG")
        self.query_tag = ""

    def _expand(self, sql: str, cut: dict[str, int]) -> str:
        def repl(m):
            ref = f"{m.group(1)}/{m.group(2)}"
            view = self.engine.port_views.get(ref)
            if view is None or ref not in cut:
                raise EngineError(f"no product snapshot view or publication bound for {ref}")
            return view.format(publication_seq=int(cut[ref]))
        return PORT_RE.sub(repl, sql)

    @staticmethod
    def _binds(sql: str) -> str:
        return re.sub(r":([a-z_][a-z0-9_]*)", r"%(\1)s", sql)

    def fetch_all(self, sql: str, params: dict[str, Any] | None = None, *, cut: dict[str, int] | None = None) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._con.cursor(__import__("snowflake.connector").connector.DictCursor)
            cur.execute(self._binds(self._expand(sql, cut or {})), params or {})
            return [{k.lower(): v for k, v in r.items()} for r in cur.fetchall()]

    def submit_async(self, sql: str, params: dict[str, Any], *, kind: str, cut: dict[str, int], output_relation: str | None = None) -> str:
        if output_relation:
            validate_relation_name(output_relation)
        try:
            with self._lock:
                cur = self._con.cursor()
                cur.execute_async(self._binds(self._expand(sql, cut)), params)
                return cur.sfqid
        except Exception as exc:  # noqa: BLE001 — connection loss after send is ambiguous
            raise SubmissionAckLost(str(exc)) from exc

    def status(self, query_id: str) -> QueryRecord:
        return self.engine.query_record(query_id, self)

    def cancel(self, query_id: str) -> None:
        with self._lock:
            self._con.cursor().execute("SELECT SYSTEM$CANCEL_QUERY(%(q)s)", {"q": query_id})

    def close(self) -> None:
        self._con.close()


class SnowflakeEngine:
    engine_name = "snowflake"
    dialect = "snowflake"

    def __init__(self, registry, connect_args: dict[str, Any], port_views: dict[str, str],
                 token_provider: Callable[[str], str | None]):
        self.registry = registry
        self.connect_args = connect_args
        self.port_views = port_views
        self.token_provider = token_provider
        self._pool: dict[tuple[str, str], list[SnowflakeSession]] = {}
        self._lock = threading.Lock()
        self.faults: dict[str, Any] = {"lose_ack": [], "fail": [], "delay": {}}

    def token_expired(self, principal: str) -> bool:
        return self.token_provider(principal) is None

    def acquire(self, principal: str, purpose: str) -> SnowflakeSession:
        with self._lock:
            idle = self._pool.setdefault((principal, purpose), [])
            if idle:
                return idle.pop()
        return SnowflakeSession(self, principal, purpose)

    def release(self, sess: SnowflakeSession) -> None:
        sess.reset()
        with self._lock:
            self._pool.setdefault((sess.principal, sess.purpose), []).append(sess)

    def query_record(self, query_id: str, sess: SnowflakeSession | None = None) -> QueryRecord:
        sess = sess or next((s for v in self._pool.values() for s in v), None)
        if sess is None:
            raise EngineError("no session available to read query status")
        st = sess._con.get_query_status(query_id)
        name = getattr(st, "name", str(st))
        mapped = {"SUCCESS": "SUCCESS", "FAILED_WITH_ERROR": "FAILED", "ABORTED": "CANCELLED", "ABORTING": "RUNNING"}.get(name, "RUNNING")
        return QueryRecord(query_id=query_id, session_id=sess.session_id, principal=sess.principal, query_tag=sess.query_tag,
                           kind="unknown", sql_hash="", status=mapped, started_at="")

    def query_history_by_tag(self, principal: str, tag: str, result_limit: int = 1000, include_running: bool = True):
        sess = self.acquire(principal, "reconciliation")
        try:
            rows = sess.fetch_all("SELECT query_id, query_tag, execution_status, session_id FROM TABLE("
                                  "INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER(RESULT_LIMIT => :lim))", {"lim": result_limit})
        finally:
            self.release(sess)
        complete = len(rows) < result_limit
        recs = [QueryRecord(query_id=r["query_id"], session_id=str(r["session_id"]), principal=principal, query_tag=r["query_tag"],
                            kind="unknown", sql_hash="", status=r["execution_status"], started_at="") for r in rows if r["query_tag"] == tag]
        return recs, complete

    def operator_stats(self, query_id: str) -> dict[str, Any]:
        return {"state": "requested", "reason": "collect GET_QUERY_OPERATOR_STATS after completion (14-day retention; warehouse privileges)"}

    def relation_exists(self, name: str) -> bool:
        raise NotImplementedError("use a governed stage registry query in the target deployment")
