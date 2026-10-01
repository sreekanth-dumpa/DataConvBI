from ipr.canonical import fingerprint, jcs, normalize_semantic
from ipr.intent import IntentBody, IntentRecord, Requester, Window, apply_edits, semantic_hash, Confirmation


def test_jcs_sorting_and_numbers():
    assert jcs({"b": 1, "a": [True, None, 1.0, 0.5, "x"]}) == '{"a":[true,null,1,0.5,"x"],"b":1}'
    assert jcs({"é": 1, "z": 2}) == '{"z":2,"é":1}'


def test_normalization_timestamps_sets_units():
    a = normalize_semantic({"knowledge_cutoff": "2026-09-15T23:59:59-04:00", "dimensions": ["channel", "product_version"],
                            "window": {"unit": "Local_Calendar_Day"}})
    assert a["knowledge_cutoff"] == "2026-09-16T03:59:59Z"
    assert a["dimensions"] == ["channel", "product_version"]
    assert a["window"]["unit"] == "local_calendar_day"
    assert fingerprint({"dimensions": ["product_version", "channel"]}) == fingerprint({"dimensions": ["channel", "product_version"]})


def _rec():
    return IntentRecord(request_id="r", conversation_id="c", requester=Requester(subject="s"),
                        intent=IntentBody(metric="m", scope={"state": "NC"}, window=Window(value=30), dimensions=["channel"]))


def test_semantic_hash_excludes_prose_and_presentation():
    a, b = _rec(), _rec()
    b.raw_text = "different words"
    b.intent.output.row_limit = 5
    b.constraints.execution_target_credits = 99
    assert semantic_hash(a) == semantic_hash(b)


def test_semantic_edit_invalidates_confirmation():
    r = _rec()
    r.confirmation = Confirmation(status="confirmed", approved_semantic_hash=semantic_hash(r))
    r2, changed = apply_edits(r, {"window.value": 31})
    assert changed and r2.confirmation.status == "invalidated"
    r3, changed = apply_edits(r, {"output.row_limit": 50})
    assert not changed and r3.confirmation.status == "confirmed"
