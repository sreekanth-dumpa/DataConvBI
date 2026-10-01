"""Target durable runtime mapping (ADR-02) — design adapter, not integration-tested.

The certified physical graph version is immutable input to a generic workflow
interpreter. SQL submission, validation, publication and presentation run as
activities; non-deterministic planning happens before the workflow starts and
its result is persisted, so workflow replay stays deterministic. Large data
never enters workflow history; activities exchange relation references.

Requires ``pip install temporalio``. The local POC uses ``ipr.runtime.online``.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

try:
    from temporalio import activity, workflow
    from temporalio.common import RetryPolicy
except ImportError:  # pragma: no cover - optional dependency
    activity = workflow = RetryPolicy = None  # type: ignore[assignment]


if workflow is not None:  # pragma: no cover

    @activity.defn
    async def run_node(execution_id: str, node: dict[str, Any], relations: dict[str, str]) -> dict[str, Any]:
        """Persist attempt + tag, submit asynchronously, reconcile on retry
        (section 10.5); returns the validated output relation reference."""
        raise NotImplementedError("bind to ipr.runtime.online.OnlineRuntime._run_sql semantics in the target deployment")

    @workflow.defn
    class InformationPathWorkflow:
        @workflow.run
        async def run(self, execution_id: str, graph: dict[str, Any]) -> dict[str, Any]:
            done: dict[str, str] = {}
            pending = {n["id"]: n for n in graph["nodes"]}
            while pending:
                ready = [n for n in pending.values() if all(d in done for d in n["depends_on"])]
                handles = [workflow.start_activity(
                    run_node, args=[execution_id, n, done], start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=RetryPolicy(maximum_attempts=3)) for n in ready]
                for n, h in zip(ready, handles):
                    out = await h
                    done[n["id"]] = out.get("relation", "")
                    pending.pop(n["id"])
            return {"execution_id": execution_id, "relations": done}
