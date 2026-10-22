"""The runloop stashes the last escalation to last_escalation.json for the runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

RUNLOOP = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runloop.py"


@pytest.fixture
def rl():
    spec = importlib.util.spec_from_file_location("brieflow_runloop", RUNLOOP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runloop"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_write_last_escalation(rl, tmp_path):
    p = rl.write_last_escalation(
        tmp_path,
        "t",
        2,
        ["sbs"],
        {
            "classification": "unknown",
            "rule": "call_reads",
            "reason": "x",
            "should_file_issue": True,
        },
        tmp_path / "diag" / "b.txt",
    )
    d = json.loads(p.read_text())
    assert (
        d["schema"] == "brieflow-last-escalation/v1"
        and d["phase"] == "sbs"
        and d["action"]["rule"] == "call_reads"
    )


def test_write_last_escalation_path_and_multi_phase(rl, tmp_path):
    p = rl.write_last_escalation(
        tmp_path, "t", 1, ["preprocess", "sbs"], {"action": "escalate"}, tmp_path / "diag" / "b.txt"
    )
    assert p == tmp_path / ".brieflow" / rl.LAST_ESCALATION_NAME
    d = json.loads(p.read_text())
    assert d["phase"] == "preprocess,sbs"
    assert d["diag_bundle"] == str(tmp_path / "diag" / "b.txt")
    assert d["attempt"] == 1
    assert d["tag"] == "t"
    assert "written_at" in d


def test_write_last_escalation_never_raises(rl, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(rl.Path, "write_text", boom)
    # must not raise even though the write itself fails
    rl.write_last_escalation(tmp_path, "t", 1, ["sbs"], {"action": "escalate"}, tmp_path / "b.txt")
