"""Tests for resolve_phase — the scoped per-phase agent resolution orchestrator.

resolve_phase is a pure orchestrator: all side-effecting capabilities are
injected via ctx. These tests build a fake ctx and assert the sequencing
(sweep-winner patch + record, cluster N0 pause-on-empty, merge N0 notify)
without any real notebook/kernel.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def resolvers():
    spec = importlib.util.spec_from_file_location("brieflow_resolvers", SCRIPTS / "brieflow_resolvers.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_resolvers"] = mod
    spec.loader.exec_module(mod)
    return mod


class _DF:
    empty = False

    def __len__(self):
        return 3


def _base_ctx(tmp_path, phase, **over):
    nb = tmp_path / f"{phase}.py"
    nb.write_text("x = 1\n")
    notes: list = []
    patched: list = []
    recorded: list = []
    ctx = {
        "analysis_dir": tmp_path,
        "conda_env": "env",
        "lab": {"sweep_bands": {}, "sweep_tiles": {"n_tiles": 2}},
        "iv": {"params": []},
        "iv_path": tmp_path / ".brieflow" / "interview.json",
        "nb_path": nb,
        "tag": "t",
        "notify": lambda *a, **k: notes.append((a, k)),
        "select_validation_tiles_from_metadata": lambda *a, **k: {"tiles": [(1, "A1", 1), (1, "A2", 2)]},
        "list_wells_from_metadata": lambda *a, **k: ["A1", "A2", "B1"],
        "is_active": lambda row, iv: True,
        "patch_marker_block_value": lambda nb, p, v: patched.append((p, v)),
        "lab_sweep_spec": lambda lab, ph, p: lab.get("sweep_bands", {}).get(ph, {}).get(p),
        "lab_sweep_band": lambda lab, ph, p: list((lab.get("sweep_bands", {}).get(ph, {}) or {}).get(p) or []),
        "adaptive_sweep_headless": None,
        "sweep_headless": None,
        "pick_sweep_winner": lambda df: 200,
        "record_tuned_confirmed": lambda iv, p, w, notebook=None: recorded.append((p, w)),
        "save_interview": lambda iv, path: None,
        "resolve_cluster_final": lambda *a: [5, 7],
        "resolve_merge_initial_sbs_tiles": lambda *a: [1, 9, 17],
        "resolve_merge_det_range": lambda *a: [1.8, 2.2],
    }
    ctx.update(over)
    ctx["_notes"], ctx["_patched"], ctx["_recorded"] = notes, patched, recorded
    return ctx


def test_no_sweep_bands_is_noop_ok(resolvers, tmp_path):
    ctx = _base_ctx(tmp_path, "preprocess")
    res = resolvers.resolve_phase("preprocess", ctx)
    assert res == {"paused": False, "reason": None}


def test_fixed_sweep_patches_winner_and_records(resolvers, tmp_path):
    async def _sweep(nb, param, band, tiles, **kw):
        return _DF()

    ctx = _base_ctx(
        tmp_path, "sbs",
        lab={"sweep_bands": {"sbs": {"THRESHOLD_READS": [100, 200, 400]}}, "sweep_tiles": {"n_tiles": 2}},
        sweep_headless=_sweep,
    )
    res = resolvers.resolve_phase("sbs", ctx)
    assert res["paused"] is False
    assert ("THRESHOLD_READS", repr(200)) in ctx["_patched"]
    assert ("THRESHOLD_READS", 200) in ctx["_recorded"]


def test_adaptive_sweep_uses_coarse_spec(resolvers, tmp_path):
    seen = {}

    async def _adaptive(nb, param, spec, tiles, **kw):
        seen["spec"] = spec
        return _DF()

    ctx = _base_ctx(
        tmp_path, "sbs",
        lab={"sweep_bands": {"sbs": {"PEAK_WIDTH": {"coarse": [3, 7, 11]}}}, "sweep_tiles": {"n_tiles": 2}},
        adaptive_sweep_headless=_adaptive,
    )
    res = resolvers.resolve_phase("sbs", ctx)
    assert res["paused"] is False
    assert seen["spec"] == {"coarse": [3, 7, 11]}


def test_no_tiles_skips_sweep_with_note(resolvers, tmp_path):
    ctx = _base_ctx(
        tmp_path, "sbs",
        lab={"sweep_bands": {"sbs": {"THRESHOLD_READS": [100]}}, "sweep_tiles": {"n_tiles": 2}},
        select_validation_tiles_from_metadata=lambda *a, **k: {"tiles": []},
    )
    res = resolvers.resolve_phase("sbs", ctx)
    assert res["paused"] is False
    assert any("no sweep tiles" in str(a) for a, _ in ctx["_notes"])


def test_cluster_pauses_when_no_final(resolvers, tmp_path):
    ctx = _base_ctx(tmp_path, "cluster", resolve_cluster_final=lambda *a: [])
    res = resolvers.resolve_phase("cluster", ctx)
    assert res["paused"] is True
    assert "FINAL_LEIDEN_RESOLUTIONS" in res["reason"]


def test_cluster_ok_when_final_selected(resolvers, tmp_path):
    ctx = _base_ctx(tmp_path, "cluster", resolve_cluster_final=lambda *a: [5, 7])
    res = resolvers.resolve_phase("cluster", ctx)
    assert res["paused"] is False


def test_merge_notifies_initial_sbs_tiles(resolvers, tmp_path):
    ctx = _base_ctx(tmp_path, "merge")
    res = resolvers.resolve_phase("merge", ctx)
    assert res["paused"] is False
    assert any("INITIAL_SBS_TILES" in str(a) for a, _ in ctx["_notes"])


def test_merge_resolves_det_range(resolvers, tmp_path):
    # #2 regression: the merge branch invokes resolve_merge_det_range and notifies.
    called = {}

    def _det(analysis_dir, lab, iv, iv_path):
        called["yes"] = True
        return [1.8, 2.2]

    ctx = _base_ctx(tmp_path, "merge", resolve_merge_det_range=_det)
    res = resolvers.resolve_phase("merge", ctx)
    assert res["paused"] is False
    assert called.get("yes") is True
    assert any("DET_RANGE" in str(a) for a, _ in ctx["_notes"])


# ---------------------------------------------------------------------------
# C1c: aggregate/cluster TEST_WELL_1/2 + TEST_CHANNEL_COMBO auto-resolution
# ---------------------------------------------------------------------------
def _find(iv, name):
    return next(r for r in iv["params"] if r["param_name"] == name)


def _agg_iv(combos=None):
    """An interview with the aggregate C1c targets unset + an operator CHANNEL_COMBOS."""
    params = [
        {"param_name": "TEST_WELL_1", "notebook": "aggregate", "value": None},
        {"param_name": "TEST_WELL_2", "notebook": "aggregate", "value": None},
        {"param_name": "TEST_CHANNEL_COMBO", "notebook": "aggregate", "value": None},
    ]
    if combos is not None:
        params.append({"param_name": "CHANNEL_COMBOS", "notebook": "aggregate", "value": combos})
    return {"params": params}


def test_aggregate_fills_distinct_wells_and_list_combo(resolvers, tmp_path):
    combos = [["DAPI", "COXIV", "CENPA", "WGA"], ["DAPI", "CENPA"]]
    ctx = _base_ctx(tmp_path, "aggregate", iv=_agg_iv(combos))
    res = resolvers.resolve_phase("aggregate", ctx)
    assert res["paused"] is False
    iv = ctx["iv"]
    # two DISTINCT sorted wells
    assert _find(iv, "TEST_WELL_1")["value"] == "A1"
    assert _find(iv, "TEST_WELL_2")["value"] == "A2"
    assert _find(iv, "TEST_WELL_1")["value_source"] == "auto-probe"
    # aggregate wants the combo as a LIST
    cc = _find(iv, "TEST_CHANNEL_COMBO")
    assert cc["value"] == ["DAPI", "COXIV", "CENPA", "WGA"]
    assert cc["value_source"] == "auto-probe"


def test_cluster_fills_combo_as_underscore_string(resolvers, tmp_path):
    combos = [["DAPI", "COXIV", "CENPA", "WGA"], ["DAPI", "CENPA"]]
    iv = {"params": [
        {"param_name": "TEST_CHANNEL_COMBO", "notebook": "cluster", "value": None},
        {"param_name": "CHANNEL_COMBOS", "notebook": "aggregate", "value": combos},
    ]}
    ctx = _base_ctx(tmp_path, "cluster", iv=iv, resolve_cluster_final=lambda *a: [5, 7])
    res = resolvers.resolve_phase("cluster", ctx)
    assert res["paused"] is False
    cc = _find(ctx["iv"], "TEST_CHANNEL_COMBO")
    # cluster wants an underscore-joined STRING (it builds a ChCo-<combo> filename)
    assert cc["value"] == "DAPI_COXIV_CENPA_WGA"
    assert cc["value_source"] == "auto-probe"


def test_single_well_uses_same_well_for_both(resolvers, tmp_path):
    ctx = _base_ctx(
        tmp_path, "aggregate",
        iv=_agg_iv([["DAPI"]]),
        list_wells_from_metadata=lambda *a, **k: ["C3"],
    )
    resolvers.resolve_phase("aggregate", ctx)
    iv = ctx["iv"]
    assert _find(iv, "TEST_WELL_1")["value"] == "C3"
    assert _find(iv, "TEST_WELL_2")["value"] == "C3"


def test_operator_values_not_overwritten(resolvers, tmp_path):
    iv = _agg_iv([["DAPI", "WGA"]])
    _find(iv, "TEST_WELL_1")["value"] = "Z9"
    _find(iv, "TEST_WELL_1")["value_source"] = "operator"
    _find(iv, "TEST_CHANNEL_COMBO")["value"] = ["MANUAL"]
    _find(iv, "TEST_CHANNEL_COMBO")["value_source"] = "operator"
    ctx = _base_ctx(tmp_path, "aggregate", iv=iv)
    resolvers.resolve_phase("aggregate", ctx)
    # operator value survives; the unset sibling is still filled
    assert _find(iv, "TEST_WELL_1")["value"] == "Z9"
    assert _find(iv, "TEST_WELL_1")["value_source"] == "operator"
    assert _find(iv, "TEST_CHANNEL_COMBO")["value"] == ["MANUAL"]
    assert _find(iv, "TEST_WELL_2")["value"] == "A2"  # wells[1]; unset sibling still filled


def test_missing_wells_warns_without_crashing(resolvers, tmp_path):
    ctx = _base_ctx(
        tmp_path, "aggregate",
        iv=_agg_iv([["DAPI"]]),
        list_wells_from_metadata=lambda *a, **k: [],
    )
    res = resolvers.resolve_phase("aggregate", ctx)
    assert res["paused"] is False  # warn, do NOT hard-pause
    iv = ctx["iv"]
    assert _find(iv, "TEST_WELL_1")["value"] is None
    assert any("set TEST_WELL_1/2 by hand" in str(a) for a, _ in ctx["_notes"])


def test_missing_channel_combos_warns_without_crashing(resolvers, tmp_path):
    ctx = _base_ctx(tmp_path, "aggregate", iv=_agg_iv(combos=None))
    res = resolvers.resolve_phase("aggregate", ctx)
    assert res["paused"] is False
    assert _find(ctx["iv"], "TEST_CHANNEL_COMBO")["value"] is None
    assert any("CHANNEL_COMBOS missing" in str(a) for a, _ in ctx["_notes"])
