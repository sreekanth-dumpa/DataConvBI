"""Streamlit AppTest: interpret -> confirm -> resolve -> plan -> execute -> present."""
import os
import time
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")

APP = str(Path(__file__).resolve().parents[1] / "app.py")


def _click(at, label):
    btn = next(b for b in at.button if b.label == label)
    btn.click()
    at.run(timeout=60)


def test_ui_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv("IPR_HOME", str(tmp_path / "ui"))
    at = st_testing.AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception
    _click(at, "Interpret and ground")
    assert any("Request req_" in s.value and "resolved" in s.value for s in at.subheader)
    _click(at, "Confirm meaning")
    _click(at, "Resolve sources and check reuse")
    _click(at, "Plan")
    _click(at, "Execute")
    deadline = time.time() + 30
    while time.time() < deadline and not any(m.label == "Conversion (mature)" for m in at.metric):
        at.run(timeout=60)
    assert any(m.label == "Conversion (mature)" for m in at.metric)
    assert not at.exception
