from datetime import timedelta

import pytest

from ipr.service import Gap
from conftest import DEMO_Q, confirmed


def test_preference_envelope_controls_auto_admission(svc):
    svc.set_preferences("analyst_nc", {"auto_admit": True, "max_credits": 1e-9})
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    assert out["status"] == "awaiting_admission" and "exceeds preference" in out["reasons"][0]
    svc.set_preferences("analyst_nc", {"auto_admit": True, "max_credits": 5})
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    assert out["state"] == "succeeded"
    adm = [e for e in svc.trace.events(request_id=out["request_id"]) if e["stage"] == "admission"][0]
    assert adm["decision"]["admission_basis"] == "preference_envelope"


def test_deferred_execution_waits_in_queue(svc):
    rid = confirmed(svc)
    svc.prepare(rid)
    svc.plan(rid)
    later = (svc.clock.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ex = svc.execute(rid, defer_until=later)
    import time
    time.sleep(0.5)
    assert svc.runtime.status(ex)["execution"]["state"] == "queued"
    svc.clock.advance(timedelta(hours=2))
    assert svc.wait(ex)["execution"]["state"] == "succeeded"


def test_paging_and_export_reauthorize(svc):
    out = svc.ask(DEMO_Q, {"subject": "analyst_nc"})
    ex = out["execution_id"]
    p0 = svc.page_result(ex, {"subject": "analyst_nc"}, page=0, size=4)
    assert len(p0["rows"]) == 4 and p0["total"] == 6 and p0["next_page"] == 1
    p1 = svc.page_result(ex, {"subject": "analyst_nc"}, page=1, size=4)
    assert len(p1["rows"]) == 2 and p1["next_page"] is None
    exp = svc.export_result(ex, {"subject": "analyst_nc"})
    assert exp["manifest"]["rows"] == 6 and open(exp["path"]).read().count("\n") == 7
    with pytest.raises(Gap) as e:
        svc.page_result(ex, {"subject": "analyst_south"})
    assert e.value.kind == "access_denied"
