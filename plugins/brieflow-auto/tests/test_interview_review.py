"""Tests for interview_review / render_interview_review — the operator's
double-check summary of how every parameter resolved."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPTS / "brieflow_wizard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


def _iv(rows):
    return {"params": rows}


def _row(nb, name, bucket, value=None, source=None, gating=None):
    r = {"notebook": nb, "param_name": name, "bucket": bucket, "value": value,
         "value_source": source, "notes": ""}
    if gating:
        r["gating"] = gating
    return r


def test_classifies_by_provenance(bw):
    iv = _iv([
        _row("sbs", "SEGMENTATION_METHOD", "user"),                       # unfilled operator -> NEEDS ANSWER
        _row("sbs", "CHANNEL_NAMES", "user", ["DAPI"], "screen-yaml"),    # from screen.yaml
        _row("sbs", "WINDOW", "override", 2, "lab-default"),              # lab default
        _row("sbs", "SBS_DATA_FORMAT", "auto-probe"),                    # pending auto-probe
        _row("sbs", "THRESHOLD_READS", "agent-resolved"),               # pending sweep
    ])
    rev = bw.interview_review(iv)
    cats = {r["param"]: r["category"] for r in rev["rows"]}
    assert cats["SEGMENTATION_METHOD"] == "NEEDS ANSWER"
    assert cats["CHANNEL_NAMES"] == "from screen.yaml"
    assert cats["WINDOW"] == "lab default"
    assert cats["SBS_DATA_FORMAT"].startswith("pending: auto-probe")
    assert cats["THRESHOLD_READS"].startswith("pending: sweep")
    assert rev["attention"]["needs_answer"] == ["sbs.SEGMENTATION_METHOD"]
    assert "sbs.SBS_DATA_FORMAT" in rev["attention"]["runtime_pending"]


def test_inactive_rows_excluded(bw):
    iv = _iv([
        _row("sbs", "SEGMENTATION_METHOD", "user", "cellpose", "operator"),
        _row("sbs", "STARDIST_MODEL", "override", gating={"param": "SEGMENTATION_METHOD", "equals": "stardist"}),
    ])
    rev = bw.interview_review(iv)
    params = {r["param"] for r in rev["rows"]}
    assert "STARDIST_MODEL" not in params  # gated off (method is cellpose)


def test_notebook_filter(bw):
    iv = _iv([
        _row("sbs", "A", "override", 1, "lab-default"),
        _row("merge", "B", "override", 2, "lab-default"),
    ])
    rev = bw.interview_review(iv, notebook="merge")
    assert [r["param"] for r in rev["rows"]] == ["B"]


def test_note_overrides_passthrough_and_render(bw):
    iv = _iv([_row("sbs", "SEGMENTATION_METHOD", "user")])
    notes = [{"param": "SKIP_CYCLES", "suggestion": "skip a cycle", "excerpt": "first cycle bad"}]
    rev = bw.interview_review(iv, note_overrides=notes)
    assert rev["attention"]["note_overrides"] == notes
    md = bw.render_interview_review(rev)
    assert "Needs your answer" in md
    assert "SKIP_CYCLES" in md
    assert "SEGMENTATION_METHOD" in md
