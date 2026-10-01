"""Engine adapter contract shared by the local SQLite engine and the optional
Snowflake adapter.

The contract mirrors the external-engine facts the runtime must cope with
(section 10.5): asynchronous submission returns a query ID, the acknowledgement
can be lost after the engine accepted the statement, query history is
searchable by session and tag with a result limit, completed-query operator
statistics are available only after completion and may be unavailable.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

UTC = timezone.utc
PORT_RE = re.compile(r"\{\{port:([a-z0-9_]+)/([a-z0-9_]+)\}\}")
STAGE_NAME_RE = re.compile(r"^(stg|res)_[a-z0-9_]{1,120}$")


class EngineError(RuntimeError):
    pass


class SubmissionAckLost(EngineError):
    """The engine may have accepted the statement but the client lost the ack."""


class TokenExpired(EngineError):
    """Delegated requester token expired; never fall back to a service principal."""


@dataclass
class QueryRecord:
    query_id: str
    session_id: str
    principal: str
    query_tag: str
    kind: str
    sql_hash: str
    status: str  # RUNNING | SUCCESS | FAILED | CANCELLED
    started_at: str
    ended_at: str | None = None
    rows_produced: int | None = None
    elapsed_ms: float | None = None
    error: str | None = None
    output_relation: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class EngineSession(Protocol):
    session_id: str
    principal: str

    def set_query_tag(self, tag: str) -> None: ...
    def submit_async(self, sql: str, params: dict[str, Any], *, kind: str, cut: dict[str, int],
                     output_relation: str | None = None) -> str: ...
    def status(self, query_id: str) -> QueryRecord: ...
    def cancel(self, query_id: str) -> None: ...
    def fetch_all(self, sql: str, params: dict[str, Any] | None = None, *, cut: dict[str, int] | None = None) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Deterministic helper functions. The SQLite engine registers them as UDFs; the
# Snowflake dialect uses native CONVERT_TIMEZONE / DATEADD equivalents.
# ---------------------------------------------------------------------------

def _parse_z(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _fmt_z(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def udf_local_date(ts: str | None, tz: str) -> str | None:
    if ts is None:
        return None
    return _parse_z(ts).astimezone(ZoneInfo(tz)).date().isoformat()


def udf_add_local_days(ts: str | None, tz: str, n: int) -> str | None:
    """Same local wall time plus n calendar days (domain_calendar_v1 DST rule).

    A wall time that does not exist after a spring-forward gap resolves with
    fold=0 (zoneinfo default); ambiguous fall-back times take the first
    occurrence. Both behaviours are fixed by the rule id.
    """
    if ts is None:
        return None
    zone = ZoneInfo(tz)
    local = _parse_z(ts).astimezone(zone).replace(tzinfo=None) + timedelta(days=int(n))
    return _fmt_z(local.replace(tzinfo=zone, fold=0))


def udf_utc_date(ts: str | None) -> str | None:
    return None if ts is None else ts[:10]


def udf_day_start_plus(d: str | None, n: int) -> str | None:
    if d is None:
        return None
    return (date.fromisoformat(d) + timedelta(days=int(n))).isoformat() + "T00:00:00Z"


HASH_VERSION = "sha256_mod_v1"


def udf_hash_mod(key: str | None, n: int) -> int | None:
    """Stable partition hash: normalized key (strip, as text) -> sha256 -> mod n.

    NULL keys return NULL and must be rejected by the partition cover assertion.
    """
    if key is None:
        return None
    digest = hashlib.sha256(str(key).strip().encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % int(n)


def sql_hash(sql: str, params: dict[str, Any] | None = None) -> str:
    h = hashlib.sha256(sql.encode("utf-8"))
    if params:
        h.update(repr(sorted((k, repr(v)) for k, v in params.items())).encode("utf-8"))
    return h.hexdigest()[:16]


def validate_relation_name(name: str) -> str:
    if not STAGE_NAME_RE.match(name):
        raise EngineError(f"relation name not allowlisted: {name!r}")
    return name
