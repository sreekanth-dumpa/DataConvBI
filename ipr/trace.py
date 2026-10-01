"""Common trace envelope and hash-linked trace store (section 14).

Records observable decisions, evidence references, rejected alternatives,
validation results and resource measurements; never hidden chain-of-thought.
The hash chain detects modification when compared with a trusted anchor; a
writable database with recomputable hashes is *not* tamper-proof storage.
Spans export in an OpenTelemetry-compatible JSON shape.
"""

from __future__ import annotations

import uuid
from typing import Any

from .canonical import jcs, normalize_semantic, sha256_hex
from .store import Store, dumps, loads

GENESIS = "0" * 64


class TraceStore:
    def __init__(self, store: Store, clock):
        self.store = store
        self.clock = clock

    def span(self, request_id: str, stage: str, *, execution_id: str | None = None, parent_span_id: str | None = None,
             status: str = "ok", decision: dict | None = None, input_refs: dict | None = None, output_refs: dict | None = None,
             expectations: dict | None = None, observations: dict | None = None, actor: dict | None = None,
             cut_mode: str | None = None, replayable_until: str | None = None, attempt: int = 1,
             extra: dict | None = None) -> str:
        span_id = "span_" + uuid.uuid4().hex[:12]
        now = self.clock.now_z()
        event = {
            "trace_id": f"trace_{request_id}", "span_id": span_id, "parent_span_id": parent_span_id, "request_id": request_id,
            "execution_id": execution_id, "stage": stage, "attempt": attempt, "started_at": now,
            "input_refs": input_refs or {}, "decision": decision or {}, "output_refs": output_refs or {},
            "expectations": expectations or {}, "observations": observations or {}, "cut_mode": cut_mode,
            "replayable_until": replayable_until, "status": status,
            "actor": actor or {"type": "service", "version": "ipr/0.1.0"}, **(extra or {}),
        }
        event = normalize_semantic(loads(dumps(event)))
        with self.store.tx() as con:
            row = con.execute("SELECT hash FROM trace_events ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            h = sha256_hex(prev + jcs(event))
            con.execute("INSERT INTO trace_events (trace_id, span_id, parent_span_id, request_id, execution_id, stage, event_json, "
                        "prev_hash, hash, at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (event["trace_id"], span_id, parent_span_id, request_id, execution_id, stage, jcs(event), prev, h, now))
        return span_id

    def events(self, request_id: str | None = None, execution_id: str | None = None) -> list[dict[str, Any]]:
        if execution_id:
            rows = self.store.q("SELECT * FROM trace_events WHERE execution_id=? ORDER BY seq", (execution_id,))
        elif request_id:
            rows = self.store.q("SELECT * FROM trace_events WHERE request_id=? ORDER BY seq", (request_id,))
        else:
            rows = self.store.q("SELECT * FROM trace_events ORDER BY seq")
        return [{**loads(r["event_json"]), "seq": r["seq"], "hash": r["hash"], "prev_hash": r["prev_hash"]} for r in rows]

    def anchor(self) -> dict[str, Any]:
        row = self.store.one("SELECT seq, hash FROM trace_events ORDER BY seq DESC LIMIT 1")
        return row or {"seq": 0, "hash": GENESIS}

    def verify(self, anchor: dict[str, Any] | None = None) -> dict[str, Any]:
        prev = GENESIS
        for r in self.store.q("SELECT seq, event_json, prev_hash, hash FROM trace_events ORDER BY seq"):
            if r["prev_hash"] != prev or sha256_hex(prev + r["event_json"]) != r["hash"]:
                return {"valid": False, "first_bad_seq": r["seq"], "reason": "hash chain mismatch"}
            prev = r["hash"]
            if anchor and r["seq"] == anchor.get("seq") and r["hash"] != anchor.get("hash"):
                return {"valid": False, "first_bad_seq": r["seq"], "reason": "anchor mismatch"}
        return {"valid": True, "head": prev, "note": "chain consistent; tamper-evidence requires a trusted external anchor"}

    def export_otel(self, request_id: str) -> list[dict[str, Any]]:
        spans = []
        for e in self.events(request_id=request_id):
            spans.append({
                "traceId": sha256_hex(e["trace_id"])[:32], "spanId": sha256_hex(e["span_id"])[:16],
                "parentSpanId": sha256_hex(e["parent_span_id"])[:16] if e.get("parent_span_id") else None,
                "name": e["stage"], "startTimeUnixNano": None, "status": {"code": "OK" if e["status"] in ("ok", "proposed") else "ERROR"},
                "attributes": {"ipr.request_id": e["request_id"], "ipr.execution_id": e.get("execution_id"),
                               "ipr.status": e["status"], "ipr.cut_mode": e.get("cut_mode")},
            })
        return spans
