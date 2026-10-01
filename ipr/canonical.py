"""Typed normalization, RFC 8785 (JCS) canonicalization and SHA-256 fingerprints.

JCS serialization alone does not normalize business meaning (section 4.1), so
``normalize_semantic`` first applies a versioned schema: timestamps become UTC
``Z`` strings, set-valued fields are sorted, units are lower-cased and decimals
are typed strings. Only then is the payload serialized with JCS and hashed.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

NORMALIZATION_SCHEMA = "semantic-normalization/v1"

# Fields whose values are sets: order carries no meaning.
SET_FIELDS = frozenset({"domains", "exclusions", "evidence_claims"})
# Fields that are lists but order-significant (e.g. output grain order shapes charts
# but not meaning); we treat dimensions as sets for meaning.
SET_LIKE_SEMANTIC = frozenset({"dimensions", "output_grain"})
UNIT_FIELDS = frozenset({"unit"})


def _jcs_number(value: float | int) -> str:
    if isinstance(value, bool):  # pragma: no cover - guarded by caller
        raise TypeError("bool is not a number")
    if isinstance(value, int):
        return str(value)
    if math.isnan(value) or math.isinf(value):
        raise ValueError("JCS forbids NaN/Infinity")
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e21:
        return str(int(value))
    # ECMAScript Number.prototype.toString is the shortest round-trip repr.
    text = repr(value)
    if "e" in text or "E" in text:
        mantissa, exp = text.lower().split("e")
        exp_i = int(exp)
        sign = "+" if exp_i > 0 else "-"
        return f"{mantissa}e{sign}{abs(exp_i)}"
    return text


def _jcs_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def jcs(value: Any) -> str:
    """Serialize ``value`` according to RFC 8785 JSON Canonicalization Scheme."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _jcs_number(value)
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        # RFC 8785: sort by UTF-16 code units of the keys.
        items = sorted(value.items(), key=lambda kv: kv[0].encode("utf-16-be"))
        return "{" + ",".join(_jcs_string(k) + ":" + jcs(v) for k, v in items) + "}"
    raise TypeError(f"Unsupported JCS type: {type(value).__name__}")


def to_utc_z(value: datetime | str) -> str:
    """Normalize a timestamp to ``YYYY-MM-DDTHH:MM:SSZ``."""
    if isinstance(value, str):
        value = parse_ts(value)
    if value.tzinfo is None:
        raise ValueError("naive timestamps are not admissible in semantic payloads")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> datetime:
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without zone: {text}")
    return dt


def _looks_like_ts(key: str, value: str) -> bool:
    return (key.endswith("_at") or key in {"knowledge_cutoff", "cutoff"}) and "T" in value


def normalize_semantic(value: Any, key: str = "") -> Any:
    """Apply the versioned typed normalization before canonical hashing."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return to_utc_z(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        # Precision-sensitive decimals must be typed strings; floats are kept
        # only when integral (e.g. 30.0 -> 30).
        if value.is_integer():
            return int(value)
        return format(Decimal(repr(value)), "f")
    if isinstance(value, str):
        if key in UNIT_FIELDS:
            return value.strip().lower()
        if _looks_like_ts(key, value):
            return to_utc_z(value)
        return value.strip()
    if isinstance(value, dict):
        return {k: normalize_semantic(v, k) for k, v in value.items() if v is not None or k == "parent"}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [normalize_semantic(v, key) for v in value]
        if key in SET_FIELDS or key in SET_LIKE_SEMANTIC or isinstance(value, (set, frozenset)):
            items = sorted(items, key=jcs)
        return items
    return value


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def fingerprint(value: Any, normalize: bool = True) -> str:
    payload = normalize_semantic(value) if normalize else value
    return "sha256:" + sha256_hex(jcs(payload))


def short(fp: str, n: int = 12) -> str:
    return fp.split(":", 1)[-1][:n]


def stable_json(value: Any) -> str:
    """Deterministic JSON for storage (not necessarily JCS)."""
    return json.dumps(value, sort_keys=True, default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return to_utc_z(o)
    if isinstance(o, date):
        return o.isoformat()
    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


def merkle_chain(items: Iterable[str], seed: str = "") -> str:
    h = seed
    for item in items:
        h = sha256_hex(h + item)
    return h
