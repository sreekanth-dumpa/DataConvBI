"""Synthetic foundational-data-product fixture.

Two authorized product ports are simulated in the data-plane database:

* ``quote_fdp/quoted_opportunities_v2`` -> base relation ``quote_branches``
  (one row per PolicyPeriod-like quote branch, linked to a submission opportunity)
* ``policy_fdp/known_bind_events_v2``   -> base relation ``bind_events``

Rows are bitemporal: ``knowledge_at`` / ``superseded_knowledge_at`` give the
knowledge interval and ``first_pub_seq`` places each row in the product
publication in which it first became visible. ``product_publications`` holds
publication metadata (snapshot time, knowledge-complete watermark, completeness).

``EDGE_CASES`` are hand-constructed submissions with independently
hand-labeled expected outcomes (``EDGE_LABELS``) for the target recipe
(gross_submission_quote_conversion 2.0.0) under the default demo parameters.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .canonical import fingerprint, parse_ts, to_utc_z

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

PUBLICATIONS = [
    # seq, snapshot_at, knowledge_complete_through, complete
    (1, "2026-09-10T04:05:00Z", "2026-09-10T04:00:00Z", 1),
    (2, "2026-09-16T04:05:00Z", "2026-09-16T04:00:00Z", 1),
    (3, "2026-09-20T04:05:00Z", "2026-09-20T04:00:00Z", 1),
]

# Default demo parameters the hand labels refer to.
DEMO_PARAMS = {
    "state": "NC", "lob": "personal_auto", "cohort_start": "2026-08-01", "cohort_end_exclusive": "2026-09-01",
    "window_value": 30, "knowledge_cutoff": "2026-09-16T03:59:59Z",
}

SUBJECTS = {
    # subject -> (allowed states, purposes)
    "analyst_nc": (["NC"], ["pricing_review"]),
    "analyst_nc_2": (["NC"], ["pricing_review"]),
    "analyst_south": (["NC", "SC", "VA"], ["pricing_review"]),
    "analyst_sc": (["SC"], ["pricing_review"]),
}


def ny(text: str) -> str:
    """Local America/New_York wall time -> UTC Z string."""
    return to_utc_z(datetime.fromisoformat(text).replace(tzinfo=NY))


@dataclass
class EdgeBranch:
    branch: str
    job_version: int
    quoted_local: str
    product_version: str = "PV2025"
    channel: str = "agent"
    status: str = "quoted"
    quality: str = "ok"
    state: str = "NC"


@dataclass
class EdgeBind:
    bind_id: str
    branch: str
    bound_local: str
    status: str = "committed"
    knowledge_local: str | None = None
    cancelled_local: str | None = None
    superseded_knowledge_local: str | None = None


EDGE_CASES: dict[str, tuple[list[EdgeBranch], list[EdgeBind], str]] = {
    "EDGE-01": ([EdgeBranch("B1", 1, "2026-08-03T10:00:00", "PV2025", "agent"), EdgeBranch("B2", 2, "2026-08-04T11:00:00", "PV2026", "direct")],
                [EdgeBind("K1", "B2", "2026-08-10T12:00:00")], "bind on a later branch converts the submission; dims from first branch"),
    "EDGE-02": ([EdgeBranch("B1", 1, "2026-08-05T09:00:00")], [EdgeBind("K1", "B1", "2026-09-04T09:00:00")], "bind exactly at window end is outside [start,end)"),
    "EDGE-03": ([EdgeBranch("B1", 1, "2026-08-05T09:00:00")], [EdgeBind("K1", "B1", "2026-09-04T08:59:59")], "bind one second before window end converts"),
    "EDGE-04": ([EdgeBranch("B1", 1, "2026-07-31T23:30:00")], [], "late-evening NC quote: local July 31 (UTC Aug 1)"),
    "EDGE-05": ([EdgeBranch("B1", 1, "2026-08-31T22:00:00", "PV2026", "direct")], [], "local Aug 31 (UTC Sep 1); immature at cutoff"),
    "EDGE-06": ([EdgeBranch("B1", 1, "2026-08-10T10:00:00")], [EdgeBind("K1", "B1", "2026-08-20T10:00:00", cancelled_local="2026-09-01T10:00:00")], "later cancellation does not reverse gross conversion"),
    "EDGE-07": ([EdgeBranch("B1", 1, "2026-08-06T10:00:00")], [EdgeBind("K1", "B1", "2026-08-12T10:00:00", knowledge_local="2026-09-17T09:00:00")], "bind known after cutoff is future knowledge"),
    "EDGE-08": ([EdgeBranch("B1", 1, "2026-08-07T10:00:00", quality="missing_rating_basis")], [EdgeBind("K1", "B1", "2026-08-08T10:00:00")], "quality-excluded first branch"),
    "EDGE-10": ([EdgeBranch("B1", 1, "2026-08-08T10:00:00")], [EdgeBind("K1", "B1", "2026-08-09T10:00:00"), EdgeBind("K2", "B1", "2026-08-11T10:00:00")], "two binds count once"),
    "EDGE-11": ([EdgeBranch("B1", 1, "2026-08-09T10:00:00")], [EdgeBind("K1", "B1", "2026-08-10T10:00:00", status="withdrawn")], "withdrawn bind does not count"),
    "EDGE-12": ([EdgeBranch("B1", 2, "2026-08-11T10:00:00", "PV2026", "direct"), EdgeBranch("B2", 1, "2026-08-11T10:00:00", "PV2025", "partner")], [], "equal quote time: tie-break by job_version"),
    "EDGE-13": ([EdgeBranch("B1", 1, "2026-08-12T08:00:00", "PV2025", "agent", status="draft"), EdgeBranch("B2", 2, "2026-08-12T15:00:00", "PV2026", "agent")],
                [EdgeBind("K1", "B2", "2026-09-11T14:00:00")], "draft branch ignored; anchor is first quoted branch"),
    "EDGE-14": ([EdgeBranch("B1", 1, "2026-08-13T10:00:00", state="SC")], [EdgeBind("K1", "B1", "2026-08-14T10:00:00")], "out of NC scope"),
    "EDGE-15": ([EdgeBranch("B1", 1, "2026-08-20T10:00:00")], [EdgeBind("K1", "B1", "2026-08-25T10:00:00")], "immature: window ends after cutoff"),
    "EDGE-16": ([EdgeBranch("B1", 1, "2026-08-14T10:00:00")],
                [EdgeBind("K1", "B1", "2026-08-15T10:00:00", knowledge_local="2026-08-15T12:00:00", superseded_knowledge_local="2026-08-16T12:00:00"),
                 EdgeBind("K2", "B1", "2026-09-20T10:00:00", knowledge_local="2026-08-16T12:00:00")], "bind corrected before cutoff to a date outside the window"),
}

# Independently hand-labeled outcomes for gross_submission_quote_conversion 2.0.0
# with DEMO_PARAMS. class in {not_in_cohort, not_in_scope, immature,
# quality_excluded, mature_converted, mature_not_converted}.
EDGE_LABELS: dict[str, dict[str, str]] = {
    "EDGE-01": {"class": "mature_converted", "product_version": "PV2025", "channel": "agent"},
    "EDGE-02": {"class": "mature_not_converted", "product_version": "PV2025", "channel": "agent"},
    "EDGE-03": {"class": "mature_converted", "product_version": "PV2025", "channel": "agent"},
    "EDGE-04": {"class": "not_in_cohort"},
    "EDGE-05": {"class": "immature", "product_version": "PV2026", "channel": "direct"},
    "EDGE-06": {"class": "mature_converted", "product_version": "PV2025", "channel": "agent"},
    "EDGE-07": {"class": "mature_not_converted", "product_version": "PV2025", "channel": "agent"},
    "EDGE-08": {"class": "quality_excluded", "product_version": "PV2025", "channel": "agent"},
    "EDGE-10": {"class": "mature_converted", "product_version": "PV2025", "channel": "agent"},
    "EDGE-11": {"class": "mature_not_converted", "product_version": "PV2025", "channel": "agent"},
    "EDGE-12": {"class": "mature_not_converted", "product_version": "PV2025", "channel": "partner"},
    "EDGE-13": {"class": "mature_converted", "product_version": "PV2026", "channel": "agent"},
    "EDGE-14": {"class": "not_in_scope"},
    "EDGE-15": {"class": "immature", "product_version": "PV2025", "channel": "agent"},
    "EDGE-16": {"class": "mature_not_converted", "product_version": "PV2025", "channel": "agent"},
}

# Legacy (quote_conversion 1.0.0) cohort membership differences at UTC dates.
EDGE_LEGACY_COHORT = {"EDGE-04": True, "EDGE-05": False}

SCHEMA = """
CREATE TABLE IF NOT EXISTS quote_branches (
  quote_branch_id TEXT PRIMARY KEY, submission_opportunity_id TEXT NOT NULL, job_version INTEGER NOT NULL,
  state TEXT NOT NULL, lob TEXT NOT NULL, product_version TEXT NOT NULL, channel TEXT NOT NULL,
  quote_status TEXT NOT NULL, quality_flag TEXT NOT NULL, quote_generated_at TEXT NOT NULL,
  knowledge_at TEXT NOT NULL, superseded_knowledge_at TEXT, first_pub_seq INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_qb_sub ON quote_branches(submission_opportunity_id);
CREATE TABLE IF NOT EXISTS bind_events (
  bind_event_id TEXT PRIMARY KEY, quote_branch_id TEXT NOT NULL, bind_status TEXT NOT NULL,
  bound_at TEXT NOT NULL, cancelled_at TEXT, knowledge_at TEXT NOT NULL, superseded_knowledge_at TEXT,
  state TEXT NOT NULL, first_pub_seq INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS ix_be_branch ON bind_events(quote_branch_id);
CREATE TABLE IF NOT EXISTS product_publications (
  product_id TEXT, port_id TEXT, publication_id TEXT PRIMARY KEY, seq INTEGER, snapshot_at TEXT,
  knowledge_complete_through TEXT, complete INTEGER, contract_version TEXT);
CREATE TABLE IF NOT EXISTS policy_state_mapping (subject TEXT, state TEXT, purpose TEXT);
CREATE TABLE IF NOT EXISTS policy_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS fixture_meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _pub_seq_for(knowledge_at: str) -> int:
    for seq, _snap, watermark, _c in PUBLICATIONS:
        if knowledge_at <= watermark:
            return seq
    return PUBLICATIONS[-1][0] + 1  # not yet published in the fixture horizon


def _edge_rows():
    branches, binds = [], []
    for sub_id, (bl, kl, _desc) in EDGE_CASES.items():
        for b in bl:
            qat = ny(b.quoted_local)
            kat = to_utc_z(parse_ts(qat) + timedelta(minutes=15))
            branches.append((f"{sub_id}-{b.branch}", sub_id, b.job_version, b.state, "personal_auto", b.product_version, b.channel,
                             b.status, b.quality, qat, kat, None, _pub_seq_for(kat)))
        for k in kl:
            bat = ny(k.bound_local)
            kat = ny(k.knowledge_local) if k.knowledge_local else to_utc_z(parse_ts(bat) + timedelta(minutes=30))
            sup = ny(k.superseded_knowledge_local) if k.superseded_knowledge_local else None
            canc = ny(k.cancelled_local) if k.cancelled_local else None
            state = next(b.state for b in bl if b.branch == k.branch)
            binds.append((f"{sub_id}-{k.bind_id}", f"{sub_id}-{k.branch}", k.status, bat, canc, kat, sup, state, _pub_seq_for(kat)))
    return branches, binds


def _random_rows(n_submissions: int, seed: int):
    rng = random.Random(seed)
    branches, binds = [], []
    start = datetime(2026, 7, 15, tzinfo=NY)
    span_minutes = int((datetime(2026, 9, 12, tzinfo=NY) - start).total_seconds() // 60)
    rollout = datetime(2026, 8, 15, tzinfo=NY)
    for i in range(n_submissions):
        sub = f"SUB-{i:07d}"
        t0 = start + timedelta(minutes=rng.randrange(span_minutes))
        state = rng.choices(["NC", "SC", "VA"], [0.85, 0.10, 0.05])[0]
        lob = rng.choices(["personal_auto", "homeowners"], [0.92, 0.08])[0]
        channel = rng.choices(["agent", "direct", "partner"], [0.55, 0.30, 0.15])[0]
        if t0 >= rollout:
            pv = rng.choices(["PV2026", "PV2025"], [0.7, 0.3])[0]
        else:
            pv = rng.choices(["PV2026", "PV2025"], [0.05, 0.95])[0]
        heavy = i % 997 == 13  # rare heavy-hitter submissions (skew)
        n_br = 6 if heavy else rng.choices([1, 2, 3, 5], [0.55, 0.30, 0.12, 0.03])[0]
        br_ids = []
        t = t0
        for j in range(n_br):
            if j:
                t = t + timedelta(minutes=rng.randrange(1, 72 * 60))
            status = "draft" if (j == 0 and rng.random() < 0.06) else "quoted"
            quality = "missing_rating_basis" if rng.random() < 0.03 else "ok"
            kat = t.astimezone(UTC) + timedelta(minutes=rng.randrange(5, 360))
            if rng.random() < 0.02:
                kat += timedelta(days=rng.randrange(2, 8))
            qat = to_utc_z(t)
            kz = to_utc_z(kat)
            bid = f"{sub}-B{j + 1}"
            br_ids.append((bid, t))
            branches.append((bid, sub, j + 1, state, lob, pv if j == 0 or rng.random() < 0.8 else "PV2026", channel,
                             status, quality, qat, kz, None, _pub_seq_for(kz)))
        p = 0.32 + (0.05 if pv == "PV2026" else 0) - (0.08 if channel == "direct" else 0)
        n_binds = rng.randrange(25, 40) if heavy else (1 if rng.random() < p else 0)
        if n_binds and rng.random() < 0.08:
            n_binds += 1
        for k in range(n_binds):
            bid, bt = rng.choice(br_ids[-2:]) if len(br_ids) > 1 else br_ids[0]
            bound = bt + timedelta(hours=rng.expovariate(1 / (24 * 7)))
            status = "withdrawn" if rng.random() < 0.05 else "committed"
            kat = bound.astimezone(UTC) + timedelta(minutes=rng.randrange(10, 720))
            if rng.random() < 0.03:
                kat += timedelta(days=rng.randrange(3, 7))
            canc = to_utc_z(bound + timedelta(days=rng.randrange(5, 40))) if rng.random() < 0.07 else None
            kz = to_utc_z(kat)
            sup = None
            row_id = f"{sub}-K{k + 1}"
            if rng.random() < 0.01:
                # correction: superseded later by a replacement row with a shifted bind time
                sup_at = kat + timedelta(days=rng.randrange(1, 10))
                sup = to_utc_z(sup_at)
                new_bound = bound + timedelta(days=rng.randrange(-3, 20))
                binds.append((row_id + "c", bid, status, to_utc_z(new_bound), canc, sup, None, state, _pub_seq_for(sup)))
            binds.append((row_id, bid, status, to_utc_z(bound), canc, kz, sup, state, _pub_seq_for(kz)))
    return branches, binds


def build_fixture(path: Path, n_submissions: int = 2400, seed: int = 20260916, force: bool = False) -> str:
    """Create the data-plane fixture database. Returns the fixture fingerprint."""
    path = Path(path)
    if path.exists() and not force:
        con = sqlite3.connect(path)
        try:
            row = con.execute("SELECT value FROM fixture_meta WHERE key='fingerprint'").fetchone()
            params = con.execute("SELECT value FROM fixture_meta WHERE key='params'").fetchone()
            if row and params and params[0] == f"{n_submissions}:{seed}":
                return row[0]
        except sqlite3.Error:
            pass
        finally:
            con.close()
    if path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    eb, ek = _edge_rows()
    rb, rk = _random_rows(n_submissions, seed)
    con.executemany("INSERT INTO quote_branches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", eb + rb)
    con.executemany("INSERT INTO bind_events VALUES (?,?,?,?,?,?,?,?,?)", ek + rk)
    for product_id, port_id in (("quote_fdp", "quoted_opportunities_v2"), ("policy_fdp", "known_bind_events_v2")):
        prefix = "quote" if product_id == "quote_fdp" else "policy"
        for seq, snap, wm, complete in PUBLICATIONS:
            pub_id = f"{prefix}_pub_{snap[:10].replace('-', '')}_{snap[11:16].replace(':', '')}"
            con.execute("INSERT INTO product_publications VALUES (?,?,?,?,?,?,?,?)", (product_id, port_id, pub_id, seq, snap, wm, complete, "2"))
    for subject, (states, purposes) in SUBJECTS.items():
        for s in states:
            for p in purposes:
                con.execute("INSERT INTO policy_state_mapping VALUES (?,?,?)", (subject, s, p))
    con.execute("INSERT INTO policy_meta VALUES ('policy_version', 'rap_state_by_subject_v3')")
    con.execute("INSERT INTO policy_meta VALUES ('entitlement_epoch', '1')")
    digest = fingerprint({"branches": len(eb + rb), "binds": len(ek + rk), "seed": seed, "n": n_submissions,
                          "sample": [r[0] for r in (eb + rb)[:50]]})
    con.execute("INSERT INTO fixture_meta VALUES ('fingerprint', ?)", (digest,))
    con.execute("INSERT INTO fixture_meta VALUES ('params', ?)", (f"{n_submissions}:{seed}",))
    con.commit()
    con.close()
    return digest
