"""Independent reference oracle.

A plain-Python implementation of both installed recipe algebras that reads raw
base rows and never uses the compiler, generated SQL or SQLite UDFs. It exists
so that every admitted physical plan can be compared with an independent
expected result on the same fixed source/access cut (section 21).
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

UTC = timezone.utc


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _z(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _visible(row: dict, seq: int, cutoff: str, states: set[str]) -> bool:
    if row["first_pub_seq"] > seq:
        return False
    if row["knowledge_at"] > cutoff:
        return False
    if row["superseded_knowledge_at"] is not None and row["superseded_knowledge_at"] <= cutoff:
        return False
    return row["state"] in states


def oracle(db_path: str, params: dict[str, Any], identity: str, timezone_name: str,
           quote_seq: int, bind_seq: int, allowed_states: set[str]) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    branches = [dict(r) for r in con.execute("SELECT * FROM quote_branches")]
    binds = [dict(r) for r in con.execute("SELECT * FROM bind_events")]
    con.close()

    cutoff = params["knowledge_cutoff"]
    n = int(params["window_value"])
    start = date.fromisoformat(params["cohort_start"])
    end = date.fromisoformat(params["cohort_end_exclusive"])
    dims = list(params.get("dimensions", ["product_version", "channel"]))
    tz = ZoneInfo(timezone_name)

    vis_branches = [b for b in branches if _visible(b, quote_seq, cutoff, allowed_states)]
    vis_binds = [k for k in binds if _visible(k, bind_seq, cutoff, allowed_states) and k["bind_status"] == "committed"]
    branch_to_sub = {b["quote_branch_id"]: b["submission_opportunity_id"] for b in vis_branches}

    units: dict[str, dict] = {}
    if identity == "submission":
        by_sub: dict[str, list] = defaultdict(list)
        for b in vis_branches:
            if b["quote_status"] == "quoted" and b["state"] == params["state"] and b["lob"] == params["lob"]:
                by_sub[b["submission_opportunity_id"]].append(b)
        for sub, bl in by_sub.items():
            first = sorted(bl, key=lambda b: (b["quote_generated_at"], b["job_version"], b["quote_branch_id"]))[0]
            anchor = _ts(first["quote_generated_at"])
            local = anchor.astimezone(tz)
            if not (start <= local.date() < end):
                continue
            end_local = (local.replace(tzinfo=None) + timedelta(days=n)).replace(tzinfo=tz)
            window_end = _z(end_local)
            units[sub] = {"first": first, "anchor": first["quote_generated_at"], "window_end": window_end}
        binds_by_unit: dict[str, list] = defaultdict(list)
        for k in vis_binds:
            sub = branch_to_sub.get(k["quote_branch_id"])
            if sub in units:
                binds_by_unit[sub].append(k)
    else:  # legacy per-branch, UTC inclusive date window
        for b in vis_branches:
            if b["quote_status"] != "quoted" or b["state"] != params["state"] or b["lob"] != params["lob"]:
                continue
            qd = _ts(b["quote_generated_at"]).date()
            if not (start <= qd < end):
                continue
            window_end = f"{(qd + timedelta(days=n + 1)).isoformat()}T00:00:00Z"
            units[b["quote_branch_id"]] = {"first": b, "anchor": b["quote_generated_at"], "window_end": window_end, "qdate": qd}
        binds_by_unit = defaultdict(list)
        for k in vis_binds:
            if k["quote_branch_id"] in units:
                binds_by_unit[k["quote_branch_id"]].append(k)

    per_unit: dict[str, dict] = {}
    agg: dict[tuple, dict[str, int]] = defaultdict(lambda: {"eligible_all": 0, "eligible_mature": 0, "immature": 0, "quality_excluded": 0, "converted": 0})
    for uid, u in units.items():
        first = u["first"]
        key = tuple(first[d] for d in dims)
        a = agg[key]
        a["eligible_all"] += 1
        if first["quality_flag"] != "ok":
            cls = "quality_excluded"
            a["quality_excluded"] += 1
        elif u["window_end"] > cutoff:
            cls = "immature"
            a["immature"] += 1
        else:
            a["eligible_mature"] += 1
            if identity == "submission":
                conv = any(u["anchor"] <= k["bound_at"] < u["window_end"] for k in binds_by_unit.get(uid, []))
            else:
                qd = u["qdate"]
                conv = any(qd <= _ts(k["bound_at"]).date() <= qd + timedelta(days=n) for k in binds_by_unit.get(uid, []))
            cls = "mature_converted" if conv else "mature_not_converted"
            if conv:
                a["converted"] += 1
        per_unit[uid] = {"class": cls, **{d: first[d] for d in dims}}
    rows = []
    for key in sorted(agg):
        r = dict(zip(dims, key))
        r.update(agg[key])
        rows.append(r)
    return {"rows": rows, "units": per_unit}
