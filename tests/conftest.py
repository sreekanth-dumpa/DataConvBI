from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ipr.config import FixedClock, Settings  # noqa: E402
from ipr.service import IPRService  # noqa: E402

DEMO_Q = ("What was the 30-day quote conversion for NC personal auto quotes issued in August 2026 "
          "by product version and channel, as of 2026-09-15?")


@pytest.fixture
def make_service(tmp_path):
    created = []

    def _make(**kw):
        settings = Settings(home=tmp_path / f"home{len(created)}", clock=FixedClock(frozen=True), fixture_submissions=600,
                            workers=kw.pop("workers", 4), lease_seconds=kw.pop("lease_seconds", 30.0))
        svc = IPRService(settings, **kw)
        created.append(svc)
        return svc

    yield _make
    for s in created:
        s.close()


@pytest.fixture
def svc(make_service):
    return make_service()


def confirmed(svc, text=DEMO_Q, subject="analyst_nc", **edits):
    st = svc.interpret(text, {"subject": subject})
    rid = st.record.request_id
    res = svc.ground(rid)
    if edits:
        res = svc.edit(rid, edits)
    assert res.status == "resolved", (res.status, res.gaps, [q.field for q in res.questions])
    svc.confirm(rid, subject)
    return rid
