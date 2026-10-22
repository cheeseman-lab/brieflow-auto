import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_decisions as bd  # noqa: E402
import brieflow_runner as ap  # noqa: E402


def test_set_paused_emits_blocker_decision(tmp_path):
    ap.set_paused(tmp_path, "merge", "qc gate failed")
    jsonl = tmp_path / ".brieflow" / "decisions.jsonl"
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert any(
        r["kind"] == "blocker-pause"
        and r["phase"] == "merge"
        and r["why"] == "qc gate failed"
        and r["source"] == "auto"
        for r in rows
    )
    # set_paused's existing behavior is preserved: progress json still records the pause
    prog = json.loads((tmp_path / ".brieflow" / "progress.json").read_text())
    assert prog["paused"]["phase"] == "merge"


def _det_range_interview(tmp_path, monkeypatch, probe_result):
    """Minimal interview with one active, unset DET_RANGE row, and a stubbed probe."""
    iv = {
        "params": [
            {
                "param_name": "DET_RANGE",
                "notebook": "merge",
                "bucket": "user",
                "value": None,
                "value_source": None,
            }
        ]
    }
    iv_path = tmp_path / ".brieflow" / "interview.json"
    iv_path.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "screen.yaml").write_text("data: {}\n")
    monkeypatch.setattr(ap, "probe_det_range", lambda *a, **k: probe_result)
    monkeypatch.setattr(ap, "save_interview", lambda *a, **k: None)
    return iv, iv_path


def _decisions(tmp_path):
    jsonl = tmp_path / ".brieflow" / "decisions.jsonl"
    if not jsonl.exists():
        return []
    return [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]


def test_det_range_probe_failure_is_recorded_not_dropped(tmp_path, monkeypatch):
    """A probe that reads no pixel size must leave a trail.

    The row stays unset so it still counts as an operator gap, and the probe's reason
    reaches both the row and the journal. Previously the note was authored and dropped
    inside `if det:`, so merge died on an unset DET_RANGE with nothing recorded.
    """
    note = "DET_RANGE: need ND2 voxel sizes or a Phenix Index.xml both sides"
    iv, iv_path = _det_range_interview(tmp_path, monkeypatch, {"DET_RANGE": None, "notes": [note]})

    assert ap.resolve_merge_det_range(tmp_path, {}, iv, iv_path) == []

    row = iv["params"][0]
    assert row["value"] is None, "must stay unset so the gap scan still reports it"
    assert row["value_source"] is None, "setting a source would mark the row filled"
    assert note in row["notes"], "the operator gap must carry why the probe failed"

    entry = next(r for r in _decisions(tmp_path) if r["kind"] == "operator-gap")
    assert entry["phase"] == "merge"
    assert entry["why"] == note
    assert entry["inputs"]["probe_notes"] == [note]


def test_det_range_probe_failure_note_not_duplicated(tmp_path, monkeypatch):
    """Re-resolving after a failure must not append the same note twice."""
    note = "DET_RANGE: need ND2 voxel sizes or a Phenix Index.xml both sides"
    iv, iv_path = _det_range_interview(tmp_path, monkeypatch, {"DET_RANGE": None, "notes": [note]})
    ap.resolve_merge_det_range(tmp_path, {}, iv, iv_path)
    ap.resolve_merge_det_range(tmp_path, {}, iv, iv_path)
    assert iv["params"][0]["notes"].count(note) == 1


def test_det_range_success_path_still_patches_and_records(tmp_path, monkeypatch):
    """The success path is unchanged: marker patched, row confirmed, winner journaled."""
    det = [0.9, 1.1]
    iv, iv_path = _det_range_interview(
        tmp_path, monkeypatch, {"DET_RANGE": det, "notes": ["from the pixel-size ratio"]}
    )
    patched = {}
    monkeypatch.setattr(
        ap, "patch_marker_block_value", lambda nb, name, value: patched.update({name: value})
    )
    monkeypatch.setattr(
        ap,
        "record_tuned_confirmed",
        lambda iv_, name, value, notebook=None: iv_["params"][0].update(
            {"value": value, "value_source": "tuned-confirmed"}
        ),
    )

    assert ap.resolve_merge_det_range(tmp_path, {}, iv, iv_path) == det
    assert patched["DET_RANGE"] == repr(det)
    assert iv["params"][0]["value"] == det

    entry = next(r for r in _decisions(tmp_path) if r["kind"] == "sweep-winner")
    assert entry["inputs"]["det_range"] == det
    assert not any(r["kind"] == "operator-gap" for r in _decisions(tmp_path))


def test_operator_is_an_accepted_decision_source(tmp_path):
    """The launch gate journals a person's approval; 'auto' would misattribute it."""
    e = bd.record_decision(
        tmp_path,
        kind="launch-approved",
        phase="preprocess",
        choice="launch as reviewed",
        why="operator passed --approved against a matching launch_review.json",
        source="operator",
    )
    assert e["source"] == "operator"
    assert "launch as reviewed" in (tmp_path / ".brieflow" / "decisions.md").read_text()


def test_unknown_decision_source_still_rejected(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        bd.record_decision(tmp_path, kind="x", phase="sbs", choice="y", why="z", source="bogus")
