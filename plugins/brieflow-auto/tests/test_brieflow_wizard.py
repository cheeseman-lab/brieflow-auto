"""Tests for brieflow_wizard.py — interview.json helpers.

State lives in a single JSON file per screen at
`<analysis-dir>/.brieflow/interview.json`, copied from the plugin's
`data/interview_template.json`. These tests cover the I/O + gating layer;
reasoning (regex inference, role mapping) is the agent's job and is not
testable in CI.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_wizard.py"
TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "interview_template.json"


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fresh_interview(bw, tmp_path):
    dst = tmp_path / ".brieflow" / "interview.json"
    bw.init_screen_interview(TEMPLATE, dst, "test-screen")
    return dst


# ---------------------------------------------------------------------------
# init_screen_interview
# ---------------------------------------------------------------------------


def test_init_stamps_screen_id_and_created_at(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    assert i["_screen_id"] == "test-screen"
    assert i["_created_at"]
    assert i["_csv_sha"]  # carried from template
    assert isinstance(i["params"], list)
    assert i["_n_params"] == len(i["params"])


def test_init_refuses_overwrite(bw, fresh_interview):
    with pytest.raises(FileExistsError):
        bw.init_screen_interview(TEMPLATE, fresh_interview, "other-screen")


def test_init_force_overwrite(bw, fresh_interview):
    bw.init_screen_interview(TEMPLATE, fresh_interview, "replaced", force=True)
    i = bw.load_interview(fresh_interview)
    assert i["_screen_id"] == "replaced"


# ---------------------------------------------------------------------------
# filter_params / find_param
# ---------------------------------------------------------------------------


def test_filter_by_notebook(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    pre = bw.filter_params(i, notebook="preprocess")
    assert all(r["notebook"] == "preprocess" for r in pre)
    assert len(pre) > 0


def test_filter_by_bucket_single(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    user_rows = bw.filter_params(i, bucket="user")
    assert all(r["bucket"] == "user" for r in user_rows)
    # user = key-decision + screen-yaml rows (the askable surface); ~40 post-rebuild
    assert len(user_rows) >= 30


def test_filter_by_bucket_set(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    rows = bw.filter_params(i, bucket={"user", "tuned"})
    assert all(r["bucket"] in {"user", "tuned"} for r in rows)


def test_filter_notebook_and_bucket(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    rows = bw.filter_params(i, notebook="sbs", bucket="user")
    assert all(r["notebook"] == "sbs" and r["bucket"] == "user" for r in rows)


def test_find_param(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    row = bw.find_param(i, "SBS_IMAGES_DIR_FP")
    assert row is not None
    assert row["notebook"] == "preprocess"
    assert bw.find_param(i, "nonexistent_param") is None


# ---------------------------------------------------------------------------
# set_value
# ---------------------------------------------------------------------------


def test_set_value_fills_row(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    row = bw.set_value(i, "SBS_IMAGES_DIR_FP", "/data/sbs/", "operator")
    assert row["value"] == "/data/sbs/"
    assert row["value_source"] == "operator"
    assert row["set_at"]


def test_set_value_rejects_unknown_source(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    with pytest.raises(ValueError, match="unknown value_source"):
        bw.set_value(i, "SBS_IMAGES_DIR_FP", "x", "guessed")


def test_set_value_unknown_param_raises(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    with pytest.raises(KeyError):
        bw.set_value(i, "nonexistent", "x", "operator")


# ---------------------------------------------------------------------------
# is_active (gating)
# ---------------------------------------------------------------------------


def test_is_active_no_gating(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    # SBS_IMAGES_DIR_FP has no "only when" clause
    row = bw.find_param(i, "SBS_IMAGES_DIR_FP")
    assert bw.is_active(row, i) is True


def test_is_active_gating_unsatisfied_when_unfilled(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    # Find a row gated on something
    gated = [r for r in i["params"] if "only when" in (r.get("notes") or "")]
    assert gated, "expected at least one gated row in the template"
    # Before the gating param is set, gated row should be inactive
    assert bw.is_active(gated[0], i) is False


def _iv(gate_value, gating=None, notes=None):
    """A minimal 2-row interview: a GATE param at `gate_value`, and a DEP param
    gated on it (structured `gating` block or legacy `notes` clause)."""
    dep = {"param_name": "DEP", "notebook": "sbs", "value": None}
    if gating is not None:
        dep["gating"] = gating
    if notes is not None:
        dep["notes"] = notes
    return {"params": [
        {"param_name": "GATE", "notebook": "sbs", "value": gate_value},
        dep,
    ]}


def test_is_active_structured_gate_ops(bw):
    """The canonical structured `gating` block flips is_active across each op."""
    def dep_active(gate_value, gating):
        i = _iv(gate_value, gating=gating)
        return bw.is_active(bw.find_param(i, "DEP"), i)

    # equals (and the `equals` shortcut): satisfied / violated / gate-unset
    assert dep_active("tiff", {"param": "GATE", "equals": "tiff"}) is True
    assert dep_active("nd2", {"param": "GATE", "equals": "tiff"}) is False
    assert dep_active(None, {"param": "GATE", "equals": "tiff"}) is False
    # in
    assert dep_active("recomb", {"param": "GATE", "op": "in", "values": ["multi", "recomb"]}) is True
    assert dep_active("single", {"param": "GATE", "op": "in", "values": ["multi", "recomb"]}) is False
    # is_set
    assert dep_active("x", {"param": "GATE", "op": "is_set"}) is True
    assert dep_active(None, {"param": "GATE", "op": "is_set"}) is False
    # not_equals
    assert dep_active("nd2", {"param": "GATE", "op": "not_equals", "value": "tiff"}) is True
    assert dep_active("tiff", {"param": "GATE", "op": "not_equals", "value": "tiff"}) is False


def test_is_active_legacy_notes_gate(bw):
    """The legacy prose `only when X == Y` fallback still gates when no
    structured block is present."""
    def dep_active(gate_value):
        i = _iv(gate_value, notes="only when GATE == 'tiff'")
        return bw.is_active(bw.find_param(i, "DEP"), i)

    assert dep_active(None) is False   # gate unset → inactive
    assert dep_active("tiff") is True
    assert dep_active("nd2") is False


def test_real_template_structured_gating_wellformed(bw, fresh_interview):
    """Every structured gating block in the shipped template must use a known op
    and reference a gate param that actually resolves — guards real-data drift."""
    i = bw.load_interview(fresh_interview)
    valid_ops = {"equals", "not_equals", "is_set", "not_null", "in"}
    bad_op, unresolved = [], []
    for row in i["params"]:
        g = row.get("gating")
        if not (isinstance(g, dict) and g.get("param")):
            continue
        op = g.get("op") or ("equals" if "equals" in g else None)
        if op not in valid_ops:
            bad_op.append((row["param_name"], op))
        gate = bw.find_param(i, g["param"], notebook=g.get("notebook") or row.get("notebook")) \
            or bw.find_param(i, g["param"])
        if gate is None:
            unresolved.append((row["param_name"], g["param"]))
    assert not bad_op, f"structured gating with unknown op: {bad_op}"
    assert not unresolved, f"structured gating references a missing gate param: {unresolved}"


# ---------------------------------------------------------------------------
# save_interview round-trip
# ---------------------------------------------------------------------------


def test_save_load_round_trip(bw, fresh_interview):
    i = bw.load_interview(fresh_interview)
    bw.set_value(i, "SBS_IMAGES_DIR_FP", "/x/", "operator")
    bw.save_interview(i, fresh_interview)
    i2 = bw.load_interview(fresh_interview)
    assert bw.find_param(i2, "SBS_IMAGES_DIR_FP")["value"] == "/x/"


def test_save_is_atomic(bw, fresh_interview):
    """After save, the .tmp file should be gone (renamed in place)."""
    i = bw.load_interview(fresh_interview)
    bw.save_interview(i, fresh_interview)
    tmp = fresh_interview.with_suffix(fresh_interview.suffix + ".tmp")
    assert not tmp.exists()


# ---------------------------------------------------------------------------
# Template integrity
# ---------------------------------------------------------------------------


def test_template_schema(bw):
    """The shipped template has the fields downstream code expects."""
    t = json.loads(TEMPLATE.read_text())
    assert t["_schema_version"] == 1
    assert t["_csv_sha"]
    assert t["_n_params"] == len(t["params"])
    required_keys = {
        "notebook", "param_name", "bucket", "default",
        "derivation_source", "written_to_config", "notes",
        "value", "value_source", "set_at",
    }
    for row in t["params"]:
        missing = required_keys - row.keys()
        assert not missing, f"{row.get('param_name')} missing keys: {missing}"
        assert row["value"] is None
        assert row["value_source"] is None
        assert row["set_at"] is None
