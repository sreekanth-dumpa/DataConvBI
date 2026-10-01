"""Always-running local online DAG runtime (section 10.3-10.5).

A bounded local stand-in for the target Temporal + Aurora runtime (ADR-02):

* ``admit`` durably writes the execution, its nodes and an outbox row in one
  transaction and returns immediately; an outbox relay starts the workflow
  with a stable workflow id, so an API crash after commit loses nothing;
* warm worker threads claim ready nodes (BEGIN IMMEDIATE as the local analogue
  of FOR UPDATE SKIP LOCKED), with fair share across principals, critical-path
  priority, leases, heartbeats and a fencing token checked on completion (CAS);
* each SQL node persists an attempt with a structured QUERY_TAG and a
  deterministic allowlisted stage name *before* submission; a lost
  acknowledgement or expired lease enters reconciliation by tag search and
  relation inspection — never a blind resubmission;
* dependents unlock only after node assertions pass; completed-stage
  observations drive adaptation of the unstarted suffix only;
* compatible nodes can be shared across executions (owner/waiter); cancelling
  a consumer never cancels a producer still needed by others;
* expired stages are swept; published results are retained.

No cron trigger, scheduling interval or deployed DAG file is involved.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from datetime import timedelta
from typing import Any, Callable

from ..canonical import fingerprint, sha256_hex, to_utc_z
from ..engines.base import EngineError, SubmissionAckLost, TokenExpired, sql_hash, validate_relation_name
from ..store import Store, dumps, loads
from ..validation import node_assertions

TERMINAL_NODE = ("complete", "failed", "cancelled", "superseded")
ACTIVE_NODE = ("reserved", "submitted", "running", "validating", "submission_unknown")
TERMINAL_EXEC = ("succeeded", "failed", "cancelled")

RESOURCE_TOKENS = {"XSMALL": 8, "SMALL": 4, "MEDIUM": 3, "LARGE": 1, None: 16}


class OnlineRuntime:
    def __init__(self, store: Store, engine, trace, clock, *, workers: int = 4, lease_seconds: float = 30.0,
                 heartbeat_seconds: float = 0.5, poll_seconds: float = 0.02, stage_ttl_hours: int = 24,
                 max_attempts: int = 3):
        self.store = store
        self.engine = engine
        self.trace = trace
        self.clock = clock
        self.n_workers = workers
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.poll = poll_seconds
        self.stage_ttl_hours = stage_ttl_hours
        self.max_attempts = max_attempts
        self.handlers: dict[str, Callable[[dict, dict, dict], dict]] = {}
        self.adaptation_policy: Callable[[dict, dict, set[str]], dict | None] | None = None
        self.on_stage_cost: Callable[[str, dict], bool] | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._tokens = {k: threading.Semaphore(v) for k, v in RESOURCE_TOKENS.items()}
        self.faults: dict[str, Any] = {"die_after_submit": set()}
        self.worker_ids = [f"w{i}_{uuid.uuid4().hex[:4]}" for i in range(workers)]
        self.started = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self.started:
            return
        self._stop.clear()
        self._threads = [threading.Thread(target=self._relay_loop, name="outbox-relay", daemon=True),
                         threading.Thread(target=self._janitor_loop, name="janitor", daemon=True)]
        self._threads += [threading.Thread(target=self._worker_loop, args=(wid,), name=f"worker-{wid}", daemon=True)
                          for wid in self.worker_ids]
        for t in self._threads:
            t.start()
        self.started = True

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        self.started = False

    # ------------------------------------------------------------------ admission
    def admit(self, *, execution_id: str, request_id: str, plan_id: str, physical_id: str, physical: dict[str, Any],
              principal: str, purpose: str, tenant: str, cut: dict[str, Any], access: dict[str, Any],
              budget: dict[str, Any], estimate: dict[str, Any], plan_version: int = 1, share: bool = True) -> dict[str, Any]:
        t0 = time.perf_counter()
        now = self.clock.now_z()
        workflow_id = f"wf_{execution_id}"
        prio = self._priorities(physical)
        with self.store.tx() as con:
            con.execute("INSERT INTO executions (execution_id, request_id, plan_id, physical_id, workflow_id, state, principal, purpose, "
                        "tenant, priority, cut_json, access_json, budget_json, estimate_json, admitted_at, plan_version) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (execution_id, request_id, plan_id, physical_id, workflow_id, "admitted", principal, purpose, tenant,
                         0.0, dumps(cut), dumps(access), dumps(budget), dumps(estimate), now, plan_version))
            for n in physical["nodes"]:
                share_key = self._share_key(n, cut, access, physical) if n["kind"] == "sql_ctas" else None
                shared_from = None
                state = "pending"
                if share and share_key and not n["depends_on"]:
                    prod = con.execute("SELECT n.execution_id, n.node_id, n.state, n.output_relation FROM nodes n JOIN executions e "
                                       "ON e.execution_id=n.execution_id WHERE n.share_key=? AND n.shared_from IS NULL AND "
                                       "n.state NOT IN ('failed','cancelled','superseded') AND e.state NOT IN ('failed','cancelled') "
                                       "ORDER BY e.admitted_at LIMIT 1", (share_key,)).fetchone()
                    if prod:
                        shared_from = f"{prod['execution_id']}:{prod['node_id']}"
                        state = "waiting_shared"
                con.execute("INSERT INTO nodes (execution_id, node_id, spec_json, state, depends_json, priority, plan_version, "
                            "share_key, shared_from, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (execution_id, n["id"], dumps(n), state, dumps(n["depends_on"]), prio[n["id"]], plan_version,
                             share_key, shared_from, now))
            con.execute("INSERT INTO outbox (kind, payload, state, created_at) VALUES (?,?,?,?)",
                        ("start_workflow", dumps({"execution_id": execution_id, "workflow_id": workflow_id}), "pending", now))
        # shared producer may already be complete
        self._resolve_waiters_for_execution(execution_id)
        return {"execution_id": execution_id, "workflow_id": workflow_id, "admission_latency_ms": round((time.perf_counter() - t0) * 1000, 3)}

    @staticmethod
    def _share_key(n: dict, cut: dict, access: dict, physical: dict) -> str:
        return fingerprint({"sql": n["sql"], "params": n["params"], "cut": cut.get("fingerprint"),
                            "access": access.get("reuse_key"), "dialect": physical["dialect"]})

    @staticmethod
    def _priorities(physical: dict[str, Any]) -> dict[str, float]:
        children: dict[str, list[str]] = {n["id"]: [] for n in physical["nodes"]}
        for n in physical["nodes"]:
            for d in n["depends_on"]:
                children[d].append(n["id"])
        memo: dict[str, float] = {}

        def depth(i: str) -> float:
            if i not in memo:
                memo[i] = 1 + max((depth(c) for c in children[i]), default=0)
            return memo[i]
        return {i: depth(i) for i in children}

    # ------------------------------------------------------------------ outbox relay
    def _relay_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self._relay_once():
                    time.sleep(self.poll)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                time.sleep(self.poll * 5)

    def _relay_once(self) -> bool:
        with self.store.tx() as con:
            row = con.execute("SELECT * FROM outbox WHERE state='pending' OR (state='claimed' AND claimed_until<?) ORDER BY id LIMIT 1",
                              (time.time(),)).fetchone()
            if not row:
                return False
            con.execute("UPDATE outbox SET state='claimed', claimed_by='relay', claimed_until=? WHERE id=?", (time.time() + 10, row["id"]))
        payload = loads(row["payload"])
        if row["kind"] == "start_workflow":
            self._start_workflow(payload["execution_id"])
        self.store.x("UPDATE outbox SET state='done' WHERE id=?", (row["id"],))
        return True

    def _start_workflow(self, execution_id: str) -> None:
        now = self.clock.now_z()
        with self.store.tx() as con:
            ex = con.execute("SELECT state FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
            if not ex or ex["state"] != "admitted":
                return  # idempotent start
            con.execute("UPDATE executions SET state='running', started_at=? WHERE execution_id=?", (now, execution_id))
            con.execute("UPDATE nodes SET state='ready', updated_at=? WHERE execution_id=? AND state='pending' AND depends_json='[]'",
                        (str(time.time()), execution_id))
        self._unlock_dependents(execution_id)
        ex = self.store.one("SELECT request_id FROM executions WHERE execution_id=?", (execution_id,))
        self.trace.span(ex["request_id"], "workflow_started", execution_id=execution_id, decision={"workflow_id": f"wf_{execution_id}"})

    # ------------------------------------------------------------------ claiming
    def _claim(self, worker_id: str) -> dict[str, Any] | None:
        with self.store.tx() as con:
            row = con.execute("""
              SELECT n.*, e.principal, e.purpose, e.tenant, e.request_id, e.cut_json, e.access_json, e.budget_json,
                     (SELECT COUNT(*) FROM nodes n2 JOIN executions e2 ON e2.execution_id=n2.execution_id
                       WHERE e2.principal=e.principal AND n2.state IN ('reserved','submitted','running','validating')) AS principal_active
              FROM nodes n JOIN executions e ON e.execution_id=n.execution_id
              WHERE n.state IN ('ready','reconcile') AND e.state='running'
              ORDER BY principal_active ASC, n.priority DESC, e.admitted_at ASC LIMIT 1""").fetchone()
            if not row:
                return None
            fence = row["fence"] + 1
            con.execute("UPDATE nodes SET state=?, lease_owner=?, lease_expires_at=?, fence=?, attempts=attempts+?, updated_at=? "
                        "WHERE execution_id=? AND node_id=?",
                        ("reserved" if row["state"] == "ready" else "reconciling", worker_id, time.time() + self.lease_seconds, fence,
                         1 if row["state"] == "ready" else 0, str(time.time()), row["execution_id"], row["node_id"]))
        claim = dict(row)
        claim["fence"] = fence
        claim["spec"] = loads(row["spec_json"])
        claim["dispatch_latency_ms"] = _latency_ms(row["updated_at"]) if row["state"] == "ready" else None
        claim["mode"] = "reconcile" if row["state"] == "reconcile" else "execute"
        claim["attempts"] = row["attempts"] + (1 if row["state"] == "ready" else 0)
        return claim

    def _heartbeat(self, claim: dict[str, Any]) -> bool:
        n = self.store.x("UPDATE nodes SET lease_expires_at=? WHERE execution_id=? AND node_id=? AND fence=?",
                         (time.time() + self.lease_seconds, claim["execution_id"], claim["node_id"], claim["fence"]))
        return n == 1

    def _set_node(self, claim: dict[str, Any], state: str, **fields) -> bool:
        sets = ", ".join(f"{k}=?" for k in fields)
        sql = f"UPDATE nodes SET state=?{', ' + sets if sets else ''}, updated_at=? WHERE execution_id=? AND node_id=? AND fence=?"
        n = self.store.x(sql, (state, *fields.values(), str(time.time()), claim["execution_id"], claim["node_id"], claim["fence"]))
        return n == 1

    # ------------------------------------------------------------------ workers
    def _worker_loop(self, worker_id: str) -> None:
        while not self._stop.is_set():
            try:
                claim = self._claim(worker_id)
                if not claim:
                    time.sleep(self.poll)
                    continue
                rc = claim["spec"].get("resource_class")
                tok = self._tokens.get(rc, self._tokens[None])
                if not tok.acquire(timeout=0.01):
                    self._set_node(claim, "ready" if claim["mode"] == "execute" else "reconcile", lease_owner=None)
                    if claim["mode"] == "execute":
                        self.store.x("UPDATE nodes SET attempts=attempts-1 WHERE execution_id=? AND node_id=?",
                                     (claim["execution_id"], claim["node_id"]))
                    time.sleep(self.poll)
                    continue
                try:
                    self._run_claim(worker_id, claim)
                finally:
                    tok.release()
            except _WorkerDeath:
                continue  # simulated crash: abandon the claim without updates
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                time.sleep(self.poll)

    def _run_claim(self, worker_id: str, claim: dict[str, Any]) -> None:
        spec = claim["spec"]
        ex_state = self.store.one("SELECT state, cancel_requested FROM executions WHERE execution_id=?", (claim["execution_id"],))
        if ex_state["cancel_requested"] and not self._has_waiters(claim["execution_id"], claim["node_id"]):
            self._set_node(claim, "cancelled")
            self._maybe_finish(claim["execution_id"])
            return
        if spec["kind"] == "sql_ctas":
            self._run_sql(worker_id, claim)
        else:
            self._run_handler(claim)

    # ------------------------------------------------------------------ SQL nodes
    def _relation_map(self, execution_id: str) -> dict[str, str]:
        rows = self.store.q("SELECT node_id, output_relation FROM nodes WHERE execution_id=? AND state='complete'", (execution_id,))
        return {r["node_id"]: r["output_relation"] for r in rows if r["output_relation"]}

    def _resolve_sql(self, sql: str, rels: dict[str, str]) -> str:
        import re

        def repl(m):
            node = m.group(1)
            if node not in rels:
                raise EngineError(f"upstream relation for {node} not available")
            return validate_relation_name(rels[node])
        return re.sub(r"\{\{rel:([a-z0-9_]+)\}\}", repl, sql)

    def stage_name(self, execution_id: str, node_id: str, attempt_no: int) -> str:
        return validate_relation_name(f"stg_{execution_id.replace('exec_', '')}_{node_id}_{attempt_no}".lower())

    def _run_sql(self, worker_id: str, claim: dict[str, Any]) -> None:
        ex_id, node_id, spec = claim["execution_id"], claim["node_id"], claim["spec"]
        cut = loads(claim["cut_json"])
        seqs = {f"{e['product_id']}/{e['port_id']}": e["seq"] for e in cut["entries"]}
        if claim["mode"] == "reconcile":
            attempt = self.store.one("SELECT * FROM node_attempts WHERE execution_id=? AND node_id=? ORDER BY attempt_no DESC LIMIT 1",
                                     (ex_id, node_id))
            self._reconcile(claim, attempt, seqs)
            return
        prev = self.store.one("SELECT MAX(attempt_no) AS m FROM node_attempts WHERE execution_id=? AND node_id=?", (ex_id, node_id))
        attempt_no = (prev["m"] or 0) + 1  # unique per node; the retry budget is tracked separately
        attempt_id = f"att_{ex_id}_{node_id}_{attempt_no}"
        out = self.stage_name(ex_id, node_id, attempt_no)
        tag = json.dumps({"ipr": 1, "exec": ex_id, "node": node_id, "attempt": attempt_no, "kind": "ctas"}, sort_keys=True, separators=(",", ":"))
        rels = self._relation_map(ex_id)
        body = self._resolve_sql(spec["sql"], rels)
        statement = f"CREATE TABLE {out} AS {body}"
        # budget: incremental reservation before each stage
        if self.on_stage_cost is not None and not self.on_stage_cost(ex_id, spec):
            self._fail_execution(claim, f"budget reservation refused before {node_id}")
            return
        now = self.clock.now_z()
        with self.store.tx() as con:
            con.execute("INSERT INTO node_attempts (attempt_id, execution_id, node_id, attempt_no, fence, query_tag, sql_hash, params_hash, "
                        "output_relation, workflow_id, activity_id, state, submitted_at, metrics_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (attempt_id, ex_id, node_id, attempt_no, claim["fence"], tag, sql_hash(statement), sha256_hex(dumps(spec["params"]))[:16],
                         out, f"wf_{ex_id}", f"act_{node_id}_{attempt_no}", "prepared", now,
                         dumps({"dispatch_latency_ms": claim.get("dispatch_latency_ms"), "worker": worker_id})))
            con.execute("INSERT OR REPLACE INTO stages VALUES (?,?,?,?,?,?,?,?,?)",
                        (out, ex_id, node_id, attempt_id, claim["principal"], now,
                         to_utc_z(self.clock.now() + timedelta(hours=self.stage_ttl_hours)), "pending", "private_to_principal"))
        sess = self.engine.acquire(claim["principal"], claim["purpose"])
        try:
            sess.set_query_tag(tag)
            try:
                qid = sess.submit_async(statement, spec["params"], kind="ctas", cut=seqs, output_relation=out)
            except SubmissionAckLost as exc:
                self.store.x("UPDATE node_attempts SET state='submission_unknown', error=? WHERE attempt_id=?", (str(exc), attempt_id))
                self._set_node(claim, "submission_unknown")
                self.trace.span(claim["request_id"], "submission_unknown", execution_id=ex_id, status="uncertain",
                                observations={"attempt_id": attempt_id, "query_tag": tag})
                attempt = self.store.one("SELECT * FROM node_attempts WHERE attempt_id=?", (attempt_id,))
                self._reconcile(claim, attempt, seqs, session=sess)
                return
            except TokenExpired as exc:
                self.store.x("UPDATE node_attempts SET state='not_submitted', error=? WHERE attempt_id=?", (str(exc), attempt_id))
                self._pause_authorization(claim, str(exc))
                return
            self.store.x("UPDATE node_attempts SET state='submitted', query_id=?, session_id=? WHERE attempt_id=?",
                         (qid, sess.session_id, attempt_id))
            self._set_node(claim, "running")
            if node_id in self.faults["die_after_submit"]:
                self.faults["die_after_submit"].discard(node_id)
                raise _WorkerDeath()
            self._await_and_complete(claim, attempt_id, qid, out, sess)
        finally:
            self.engine.release(sess)

    def _await_and_complete(self, claim, attempt_id: str, qid: str, out: str, sess) -> None:
        ex_id, node_id, spec = claim["execution_id"], claim["node_id"], claim["spec"]
        last_hb = time.monotonic()
        cancelled_sent = False
        while True:
            rec = self.engine.query_record(qid)
            if rec.status != "RUNNING":
                break
            if time.monotonic() - last_hb > self.heartbeat_seconds:
                if not self._heartbeat(claim):
                    return  # fenced out: another worker owns this node now
                last_hb = time.monotonic()
            ex = self.store.one("SELECT cancel_requested FROM executions WHERE execution_id=?", (ex_id,))
            if ex["cancel_requested"] and not cancelled_sent and not self._has_waiters(ex_id, node_id):
                sess.cancel(qid)
                cancelled_sent = True
            time.sleep(self.poll)
        metrics = {"rows": rec.rows_produced, "elapsed_ms": rec.elapsed_ms, "query_id": qid,
                   "operator_stats": self.engine.operator_stats(qid)}
        prior = loads(self.store.one("SELECT metrics_json FROM node_attempts WHERE attempt_id=?", (attempt_id,))["metrics_json"]) or {}
        metrics = {**prior, **metrics}
        if rec.status == "SUCCESS":
            self._complete_sql(claim, attempt_id, out, metrics, sess)
        elif rec.status == "CANCELLED":
            self.store.x("UPDATE node_attempts SET state='cancelled', ended_at=?, metrics_json=?, error=? WHERE attempt_id=?",
                         (self.clock.now_z(), dumps(metrics), rec.error, attempt_id))
            self._set_node(claim, "cancelled")
            self._drop_stage(out)
            self._maybe_finish(ex_id)
        else:
            self.store.x("UPDATE node_attempts SET state='failed', ended_at=?, metrics_json=?, error=? WHERE attempt_id=?",
                         (self.clock.now_z(), dumps(metrics), rec.error, attempt_id))
            self._drop_stage(out)
            self._retry_or_fail(claim, rec.error or "query failed")

    def _complete_sql(self, claim, attempt_id: str, out: str, metrics: dict, sess) -> None:
        ex_id, node_id, spec = claim["execution_id"], claim["node_id"], claim["spec"]
        self._set_node(claim, "validating")
        verdicts = node_assertions(sess, spec, out)
        lo, hi = spec["envelope"]["rows"] if spec.get("envelope") else (None, None)
        rows = metrics.get("rows")
        metrics["envelope"] = {"expected": [lo, hi], "observed": rows,
                               "deviation": None if rows is None or lo is None else ("above" if rows > hi else "below" if rows < lo else "within")}
        metrics["assertions"] = verdicts
        failed = [a for a, v in verdicts.items() if not v["passed"]]
        self._telemetry(ex_id, node_id, attempt_id, metrics)
        if failed:
            self.store.x("UPDATE node_attempts SET state='validation_failed', ended_at=?, metrics_json=? WHERE attempt_id=?",
                         (self.clock.now_z(), dumps(metrics), attempt_id))
            self._set_node(claim, "failed", observations_json=dumps(metrics))
            self._fail_execution(claim, f"grain/obligation violation at {node_id}: {failed}")
            return
        self.store.x("UPDATE node_attempts SET state='complete', ended_at=?, metrics_json=? WHERE attempt_id=?",
                     (self.clock.now_z(), dumps(metrics), attempt_id))
        self.store.x("UPDATE stages SET state='complete' WHERE relation=?", (out,))
        if not self._set_node(claim, "complete", output_relation=out, observations_json=dumps(metrics)):
            # fenced out: a newer owner exists; this attempt cannot publish its output
            self.store.x("UPDATE node_attempts SET state='stale_fenced' WHERE attempt_id=?", (attempt_id,))
            return
        self.trace.span(claim["request_id"], f"node_complete:{node_id}", execution_id=ex_id,
                        observations={"attempt_id": attempt_id, "query_tag": None, "query_id": metrics.get("query_id"),
                                      "rows": rows, "elapsed_ms": metrics.get("elapsed_ms"),
                                      "spill_bytes": None, "spill_reason": "not observable in local engine"},
                        expectations={"output_rows_range": [lo, hi]})
        self._resolve_waiters(ex_id, node_id, out)
        if spec.get("checkpoint"):
            self._checkpoint(claim)  # adapt the unstarted suffix before dependents can start
        self._unlock_dependents(ex_id)

    def _telemetry(self, ex_id, node_id, attempt_id, metrics) -> None:
        now = self.clock.now_z()
        qid = metrics.get("query_id")
        for name in ("rows", "elapsed_ms"):
            v = metrics.get(name)
            self.store.x("INSERT INTO telemetry (execution_id, node_id, attempt_id, query_id, metric, value, state, reason, at) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (ex_id, node_id, attempt_id, qid, name, v, "collected" if v is not None else "unavailable",
                                                        None, now))
        for name in ("spill_local_bytes", "spill_remote_bytes"):
            self.store.x("INSERT INTO telemetry (execution_id, node_id, attempt_id, query_id, metric, value, state, reason, at) "
                         "VALUES (?,?,?,?,?,?,?,?,?)", (ex_id, node_id, attempt_id, qid, name, None, "unavailable",
                                                        "not observable in the local SQLite engine", now))

    # ------------------------------------------------------------------ reconciliation
    def _reconcile(self, claim, attempt: dict | None, seqs: dict, session=None) -> None:
        """Resolve an uncertain submission without blind resubmission (10.5)."""
        ex_id, node_id = claim["execution_id"], claim["node_id"]
        if attempt is None:
            self._set_node(claim, "ready")
            return
        evidence: dict[str, Any] = {"attempt_id": attempt["attempt_id"], "tag": attempt["query_tag"]}
        qid = attempt["query_id"]
        if not qid:
            recs, complete = self.engine.query_history_by_tag(claim["principal"], attempt["query_tag"], result_limit=1000)
            evidence.update(history_matches=[r.query_id for r in recs], history_window_complete=complete)
            if len(recs) == 1:
                qid = recs[0].query_id
                evidence["resolution"] = "adopted query id from tagged history"
            elif len(recs) > 1:
                evidence["resolution"] = "unresolved: multiple tagged matches"
            elif not complete:
                evidence["resolution"] = "unresolved: history window truncated"
            else:
                exists = self.engine.relation_exists(attempt["output_relation"])
                evidence["output_relation_exists"] = exists
                evidence["resolution"] = ("unresolved: relation without history" if exists
                                          else "known no external effect; new attempt permitted")
        self.store.x("UPDATE node_attempts SET reconciliation_json=? WHERE attempt_id=?", (dumps(evidence), attempt["attempt_id"]))
        self.trace.span(claim["request_id"], f"reconcile:{node_id}", execution_id=ex_id, status="ok",
                        decision={"resolution": evidence.get("resolution", "query id known")}, observations=evidence)
        if qid:
            self.store.x("UPDATE node_attempts SET query_id=?, state='submitted' WHERE attempt_id=?", (qid, attempt["attempt_id"]))
            self._set_node(claim, "running")
            sess = session or self.engine.acquire(claim["principal"], claim["purpose"])
            try:
                self._await_and_complete(claim, attempt["attempt_id"], qid, attempt["output_relation"], sess)
            finally:
                if session is None:
                    self.engine.release(sess)
            return
        if evidence["resolution"].startswith("known no external effect"):
            self.store.x("UPDATE node_attempts SET state='not_submitted' WHERE attempt_id=?", (attempt["attempt_id"],))
            self._retry_or_fail(claim, "submission had no external effect")
            return
        self.store.x("UPDATE node_attempts SET state='unresolved' WHERE attempt_id=?", (attempt["attempt_id"],))
        self._fail_execution(claim, f"submission for {node_id} unresolved: {evidence['resolution']}")

    # ------------------------------------------------------------------ handler nodes
    def _run_handler(self, claim) -> None:
        ex_id, node_id, spec = claim["execution_id"], claim["node_id"], claim["spec"]
        handler = self.handlers.get(spec["kind"])
        if handler is None:
            self._fail_execution(claim, f"no handler for node kind {spec['kind']}")
            return
        execution = self.store.one("SELECT * FROM executions WHERE execution_id=?", (ex_id,))
        context = {"relations": self._relation_map(ex_id), "fence": claim["fence"]}
        t0 = time.perf_counter()
        try:
            result = handler(execution, spec, context)
        except Exception as exc:  # noqa: BLE001
            self._set_node(claim, "failed", observations_json=dumps({"error": f"{type(exc).__name__}: {exc}"}))
            self._fail_execution(claim, f"{node_id} failed: {exc}")
            return
        result = result or {}
        result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 3)
        if not result.get("ok", True):
            self._set_node(claim, "failed", observations_json=dumps(result))
            self._fail_execution(claim, result.get("error", f"{node_id} failed"))
            return
        if not self._set_node(claim, "complete", observations_json=dumps(result)):
            return
        self._unlock_dependents(ex_id)
        if node_id == "present":
            self._finish(ex_id, "succeeded")

    # ------------------------------------------------------------------ graph progress
    def _unlock_dependents(self, execution_id: str) -> None:
        with self.store.tx() as con:
            done = {r["node_id"] for r in con.execute("SELECT node_id FROM nodes WHERE execution_id=? AND state='complete'", (execution_id,))}
            for r in con.execute("SELECT node_id, depends_json FROM nodes WHERE execution_id=? AND state='pending'", (execution_id,)).fetchall():
                if set(loads(r["depends_json"])) <= done:
                    con.execute("UPDATE nodes SET state='ready', updated_at=? WHERE execution_id=? AND node_id=? AND state='pending'",
                                (str(time.time()), execution_id, r["node_id"]))

    def _has_waiters(self, execution_id: str, node_id: str) -> bool:
        return self.store.one("SELECT 1 AS x FROM nodes WHERE shared_from=? AND state='waiting_shared'",
                              (f"{execution_id}:{node_id}",)) is not None

    def _resolve_waiters(self, execution_id: str, node_id: str, out: str) -> None:
        waiters = self.store.q("SELECT execution_id FROM nodes WHERE shared_from=? AND state='waiting_shared'", (f"{execution_id}:{node_id}",))
        if not waiters:
            return
        self.store.x("UPDATE nodes SET state='complete', output_relation=?, observations_json=?, updated_at=? "
                     "WHERE shared_from=? AND state='waiting_shared'",
                     (out, dumps({"shared_from": f"{execution_id}:{node_id}"}), str(time.time()), f"{execution_id}:{node_id}"))
        for w in waiters:
            self._unlock_dependents(w["execution_id"])

    def _resolve_waiters_for_execution(self, execution_id: str) -> None:
        for w in self.store.q("SELECT node_id, shared_from FROM nodes WHERE execution_id=? AND state='waiting_shared'", (execution_id,)):
            p_ex, p_node = w["shared_from"].split(":")
            prod = self.store.one("SELECT state, output_relation FROM nodes WHERE execution_id=? AND node_id=?", (p_ex, p_node))
            if prod and prod["state"] == "complete":
                self.store.x("UPDATE nodes SET state='complete', output_relation=?, updated_at=? WHERE execution_id=? AND node_id=?",
                             (prod["output_relation"], str(time.time()), execution_id, w["node_id"]))
            elif prod is None or prod["state"] in ("failed", "cancelled", "superseded"):
                self.store.x("UPDATE nodes SET state='pending', shared_from=NULL WHERE execution_id=? AND node_id=?", (execution_id, w["node_id"]))
        st = self.store.one("SELECT state FROM executions WHERE execution_id=?", (execution_id,))
        if st and st["state"] == "running":
            self._unlock_dependents(execution_id)

    def _release_waiters_of(self, execution_id: str) -> None:
        """Producer failed or cancelled before running: waiters run themselves."""
        rows = self.store.q("SELECT execution_id, node_id FROM nodes WHERE shared_from LIKE ? AND state='waiting_shared'", (f"{execution_id}:%",))
        for r in rows:
            self.store.x("UPDATE nodes SET state='ready', shared_from=NULL, updated_at=? WHERE execution_id=? AND node_id=?",
                         (str(time.time()), r["execution_id"], r["node_id"]))

    def _retry_or_fail(self, claim, reason: str) -> None:
        if claim["attempts"] < self.max_attempts:
            self._set_node(claim, "ready", lease_owner=None)
            self.trace.span(claim["request_id"], f"retry:{claim['node_id']}", execution_id=claim["execution_id"], status="retry",
                            decision={"reason": reason, "attempt": claim["attempts"]})
        else:
            self._set_node(claim, "failed")
            self._fail_execution(claim, f"{claim['node_id']} failed after {claim['attempts']} attempts: {reason}")

    def _pause_authorization(self, claim, reason: str) -> None:
        self._set_node(claim, "ready", lease_owner=None)
        self.store.x("UPDATE nodes SET attempts=attempts-1 WHERE execution_id=? AND node_id=?", (claim["execution_id"], claim["node_id"]))
        self.store.x("UPDATE executions SET state='paused_authorization', error=? WHERE execution_id=?", (reason, claim["execution_id"]))
        self.trace.span(claim["request_id"], "paused_authorization", execution_id=claim["execution_id"], status="paused",
                        decision={"reason": reason, "fallback": "none: never use a broader service principal"})

    def resume(self, execution_id: str) -> None:
        self.store.x("UPDATE executions SET state='running', error=NULL WHERE execution_id=? AND state='paused_authorization'", (execution_id,))

    def _fail_execution(self, claim, reason: str) -> None:
        ex_id = claim["execution_id"]
        with self.store.tx() as con:
            con.execute("UPDATE executions SET state='failed', error=?, finished_at=? WHERE execution_id=? AND state NOT IN ('succeeded','failed','cancelled')",
                        (reason, self.clock.now_z(), ex_id))
            con.execute("UPDATE nodes SET state='cancelled' WHERE execution_id=? AND state IN ('pending','ready','waiting_shared')", (ex_id,))
        self._release_waiters_of(ex_id)
        self.trace.span(claim["request_id"], "execution_failed", execution_id=ex_id, status="failed", decision={"reason": reason})

    def _finish(self, execution_id: str, state: str) -> None:
        self.store.x("UPDATE executions SET state=?, finished_at=? WHERE execution_id=? AND state NOT IN ('succeeded','failed','cancelled')",
                     (state, self.clock.now_z(), execution_id))

    def _maybe_finish(self, execution_id: str) -> None:
        ex = self.store.one("SELECT cancel_requested, request_id FROM executions WHERE execution_id=?", (execution_id,))
        active = self.store.one("SELECT COUNT(*) AS c FROM nodes WHERE execution_id=? AND state NOT IN ('complete','failed','cancelled','superseded')",
                                (execution_id,))["c"]
        if ex["cancel_requested"] and active == 0:
            self._finish(execution_id, "cancelled")
            self.trace.span(ex["request_id"], "execution_cancelled", execution_id=execution_id, status="cancelled")

    def _drop_stage(self, relation: str | None) -> None:
        if relation:
            try:
                self.engine.drop_relation(relation)
            except EngineError:
                pass
            self.store.x("UPDATE stages SET state='dropped' WHERE relation=?", (relation,))

    # ------------------------------------------------------------------ adaptation
    def _checkpoint(self, claim) -> None:
        if self.adaptation_policy is None:
            return
        ex_id = claim["execution_id"]
        ex = self.store.one("SELECT * FROM executions WHERE execution_id=?", (ex_id,))
        completed = {r["node_id"] for r in self.store.q("SELECT node_id FROM nodes WHERE execution_id=? AND state='complete'", (ex_id,))}
        obs = {r["node_id"]: loads(r["observations_json"]) for r in
               self.store.q("SELECT node_id, observations_json FROM nodes WHERE execution_id=? AND state='complete'", (ex_id,))}
        physical = loads(self.store.one("SELECT plan_json FROM physical_plans WHERE physical_id=?", (ex["physical_id"],))["plan_json"])
        proposal = self.adaptation_policy(ex, physical, {"completed": completed, "observations": obs})
        if not proposal:
            return
        new_plan, record = proposal["plan"], proposal["record"]
        with self.store.tx() as con:
            current = {r["node_id"]: r["state"] for r in con.execute("SELECT node_id, state FROM nodes WHERE execution_id=?", (ex_id,))}
            keep = {n["id"] for n in new_plan["nodes"] if n["id"] in completed}
            suffix_old = [nid for nid in current if nid not in keep]
            if any(current[n] not in ("pending", "ready") for n in suffix_old):
                record["applied"] = False
                record["reason"] = "suffix already started; adaptation abandoned"
            else:
                for nid in suffix_old:
                    con.execute("UPDATE nodes SET state='superseded' WHERE execution_id=? AND node_id=?", (ex_id, nid))
                prio = self._priorities(new_plan)
                version = (ex["plan_version"] or 1) + 1
                for n in new_plan["nodes"]:
                    if n["id"] in keep:
                        continue
                    con.execute("INSERT OR REPLACE INTO nodes (execution_id, node_id, spec_json, state, depends_json, priority, plan_version, updated_at) "
                                "VALUES (?,?,?,?,?,?,?,?)", (ex_id, n["id"], dumps(n), "pending", dumps(n["depends_on"]), prio[n["id"]], version,
                                                           str(time.time())))
                con.execute("INSERT INTO physical_plans VALUES (?,?,?,?,?,?,?,?,?)",
                            (proposal["physical_id"], ex["plan_id"], 1, new_plan["variant"], new_plan["physical_fingerprint"], dumps(new_plan),
                             dumps(proposal["certificate"]), ex["physical_id"], self.clock.now_z()))
                con.execute("UPDATE executions SET physical_id=?, plan_version=?, revisions=revisions+1 WHERE execution_id=?",
                            (proposal["physical_id"], version, ex_id))
                record["applied"] = True
                con.execute("INSERT INTO adaptations VALUES (?,?,?,?,?)", (ex_id, version, ex["plan_version"] or 1, dumps(record), self.clock.now_z()))
        self.trace.span(ex["request_id"], "adaptation", execution_id=ex_id, status="ok" if record.get("applied") else "skipped",
                        decision=record)
        self._unlock_dependents(ex_id)

    # ------------------------------------------------------------------ janitor
    def _janitor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.janitor_tick()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            time.sleep(max(self.poll * 10, 0.1))

    def janitor_tick(self) -> None:
        now = time.time()
        expired = self.store.q("SELECT n.execution_id, n.node_id, e.request_id FROM nodes n JOIN executions e ON e.execution_id=n.execution_id "
                               "WHERE n.state IN ('reserved','submitted','running','validating','submission_unknown','reconciling') "
                               "AND n.lease_expires_at < ?", (now,))
        for r in expired:
            n = self.store.x("UPDATE nodes SET state='reconcile', lease_owner=NULL WHERE execution_id=? AND node_id=? AND lease_expires_at < ? "
                             "AND state IN ('reserved','submitted','running','validating','submission_unknown','reconciling')",
                             (r["execution_id"], r["node_id"], now))
            if n:
                self.store.x("UPDATE node_attempts SET state='orphaned' WHERE execution_id=? AND node_id=? AND state IN ('prepared','submitted')",
                             (r["execution_id"], r["node_id"]))
                self.trace.span(r["request_id"], f"lease_expired:{r['node_id']}", execution_id=r["execution_id"], status="orphaned",
                                decision={"action": "reconcile before any retry"})
        # cancellation bookkeeping
        for ex in self.store.q("SELECT execution_id FROM executions WHERE cancel_requested=1 AND state NOT IN ('succeeded','failed','cancelled')"):
            self._maybe_finish(ex["execution_id"])
        # stage sweeper (business clock)
        bnow = self.clock.now_z()
        for s in self.store.q("SELECT relation FROM stages WHERE expires_at < ? AND state IN ('complete','pending')", (bnow,)):
            self._drop_stage(s["relation"])

    # ------------------------------------------------------------------ control API
    def cancel(self, execution_id: str) -> dict[str, Any]:
        with self.store.tx() as con:
            con.execute("UPDATE executions SET cancel_requested=1 WHERE execution_id=?", (execution_id,))
            # nodes with waiters keep running for their other consumers
            rows = con.execute("SELECT node_id FROM nodes WHERE execution_id=? AND state IN ('pending','ready','waiting_shared')",
                               (execution_id,)).fetchall()
            kept = []
            for r in rows:
                waiters = con.execute("SELECT 1 FROM nodes WHERE shared_from=? AND state='waiting_shared'", (f"{execution_id}:{r['node_id']}",)).fetchone()
                if waiters:
                    kept.append(r["node_id"])
                    continue
                con.execute("UPDATE nodes SET state='cancelled' WHERE execution_id=? AND node_id=?", (execution_id, r["node_id"]))
        self._maybe_finish(execution_id)
        return {"execution_id": execution_id, "cancel_requested": True, "producers_kept_for_waiters": kept}

    def status(self, execution_id: str) -> dict[str, Any]:
        ex = self.store.one("SELECT * FROM executions WHERE execution_id=?", (execution_id,))
        if not ex:
            raise KeyError(execution_id)
        nodes = self.store.q("SELECT node_id, state, attempts, fence, output_relation, observations_json, shared_from, plan_version "
                             "FROM nodes WHERE execution_id=? ORDER BY rowid", (execution_id,))
        attempts = self.store.q("SELECT attempt_id, node_id, attempt_no, state, query_id, query_tag, output_relation, metrics_json, "
                                "reconciliation_json, error FROM node_attempts WHERE execution_id=? ORDER BY submitted_at", (execution_id,))
        return {"execution": ex, "nodes": [{**n, "observations": loads(n.pop("observations_json"))} for n in nodes],
                "attempts": [{**a, "metrics": loads(a.pop("metrics_json")), "reconciliation": loads(a.pop("reconciliation_json"))}
                             for a in attempts]}

    def wait(self, execution_id: str, timeout: float = 120) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ex = self.store.one("SELECT state FROM executions WHERE execution_id=?", (execution_id,))
            if ex and ex["state"] in TERMINAL_EXEC:
                return self.status(execution_id)
            time.sleep(self.poll)
        raise TimeoutError(f"execution {execution_id} did not finish in {timeout}s")


def _latency_ms(updated_at: str | None) -> float | None:
    try:
        return round((time.time() - float(updated_at)) * 1000, 3)
    except (TypeError, ValueError):
        return None


class _WorkerDeath(Exception):
    """Simulated worker crash (fault injection)."""
