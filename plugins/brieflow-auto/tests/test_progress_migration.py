"""Progress-file migration: autopilot_progress.json -> progress.json (spec §4.4)."""

from __future__ import annotations
import importlib.util
import json
import sys
from pathlib import Path
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runner.py"


@pytest.fixture(scope="module")
def rn():
    spec = importlib.util.spec_from_file_location("brieflow_runner_pm", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runner_pm"] = m
    spec.loader.exec_module(m)
    return m


def _bf(tmp_path):
    d = tmp_path / ".brieflow"
    d.mkdir()
    return d


def test_legacy_alone_is_renamed_and_journalled(rn, tmp_path, monkeypatch):
    d = _bf(tmp_path)
    (d / "autopilot_progress.json").write_text(json.dumps({"done_phases": ["preprocess"]}))
    calls = []
    monkeypatch.setattr(rn, "record_decision", lambda *a, **k: calls.append(k))
    data = rn._read_progress(tmp_path)
    assert data["done_phases"] == ["preprocess"]
    assert (d / "progress.json").exists() and not (d / "autopilot_progress.json").exists()
    assert calls and calls[0]["kind"] == "progress-file-migrated"


def test_migration_failure_falls_back_to_legacy_read(rn, tmp_path, monkeypatch):
    d = _bf(tmp_path)
    (d / "autopilot_progress.json").write_text(json.dumps({"done_phases": ["preprocess"]}))
    calls = []
    monkeypatch.setattr(rn, "record_decision", lambda *a, **k: calls.append(k))

    def _raise_permission_error(*a, **k):
        raise PermissionError("nope")

    monkeypatch.setattr(rn.os, "replace", _raise_permission_error)
    data = rn._read_progress(tmp_path)
    assert data["done_phases"] == ["preprocess"]
    assert (d / "autopilot_progress.json").exists()
    assert not any(k.get("kind") == "progress-file-migrated" for k in calls)


def test_record_decision_failure_after_successful_rename(rn, tmp_path, monkeypatch):
    d = _bf(tmp_path)
    (d / "autopilot_progress.json").write_text(json.dumps({"done_phases": ["preprocess"]}))

    def _raise_runtime_error(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(rn, "record_decision", _raise_runtime_error)
    data = rn._read_progress(tmp_path)
    assert data["done_phases"] == ["preprocess"]
    assert (d / "progress.json").exists()
    assert not (d / "autopilot_progress.json").exists()


def test_both_present_new_wins_legacy_untouched(rn, tmp_path, monkeypatch):
    d = _bf(tmp_path)
    (d / "autopilot_progress.json").write_text(json.dumps({"done_phases": ["sbs"]}))
    (d / "progress.json").write_text(json.dumps({"done_phases": ["merge"]}))
    notes = []
    monkeypatch.setattr(rn, "notify", lambda *a, **k: notes.append((a, k)))
    monkeypatch.setattr(rn, "record_decision", lambda *a, **k: None)
    assert rn._read_progress(tmp_path)["done_phases"] == ["merge"]
    assert (d / "autopilot_progress.json").exists()
    assert any(k.get("priority") == "warning" for _, k in notes)


def test_neither_present_is_empty(rn, tmp_path):
    _bf(tmp_path)
    assert rn._read_progress(tmp_path) == {}


def test_progress_path_is_new_name(rn, tmp_path):
    assert rn.progress_path(tmp_path).name == "progress.json"
