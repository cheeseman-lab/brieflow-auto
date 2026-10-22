"""Unit tests for T6 (lab_defaults loader) and T7 (progress.json helpers).

Both live in brieflow_runner.py. These tests cover the pure-Python
utility layer; the conductor loop is tested in test_runner_loop.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runner.py"
LAB_DEFAULTS_YAML = Path(__file__).resolve().parent.parent / "data" / "lab_defaults.yaml"


@pytest.fixture(scope="module")
def ap():
    spec = importlib.util.spec_from_file_location("brieflow_runner", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runner"] = m
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------------------
# T6: lab_defaults loader + helpers
# ---------------------------------------------------------------------------


def test_load_lab_defaults_returns_dict(ap):
    d = ap.load_lab_defaults()
    assert isinstance(d, dict)


def test_load_lab_defaults_has_autonomy_mode(ap):
    d = ap.load_lab_defaults()
    assert "autonomy_mode" in d


def test_load_lab_defaults_has_sweep_bands(ap):
    d = ap.load_lab_defaults()
    assert "sweep_bands" in d
    assert isinstance(d["sweep_bands"], dict)


def test_load_lab_defaults_has_sweep_tiles(ap):
    d = ap.load_lab_defaults()
    assert "sweep_tiles" in d
    assert "n_tiles" in d["sweep_tiles"]


def test_load_lab_defaults_has_recovery(ap):
    d = ap.load_lab_defaults()
    assert "recovery" in d
    assert "max_attempts" in d["recovery"]


def test_lab_sweep_band_sbs_threshold_reads(ap):
    d = ap.load_lab_defaults()
    band = ap.lab_sweep_band(d, "sbs", "THRESHOLD_READS")
    assert isinstance(band, list)
    assert len(band) > 0


def test_lab_sweep_band_sbs_peak_width(ap):
    d = ap.load_lab_defaults()
    band = ap.lab_sweep_band(d, "sbs", "PEAK_WIDTH")
    assert isinstance(band, list)
    assert len(band) > 0


def test_lab_sweep_band_phenotype_empty(ap):
    # Phenotype diameters are cellpose-auto-estimated per screen, so there is no
    # operator-swept band: phenotype: {} -> empty list for any param.
    d = ap.load_lab_defaults()
    band = ap.lab_sweep_band(d, "phenotype", "NUCLEI_DIAMETER")
    assert band == []


def test_lab_sweep_band_absent_phase_returns_empty(ap):
    d = ap.load_lab_defaults()
    band = ap.lab_sweep_band(d, "preprocess", "NONEXISTENT_PARAM")
    assert band == []


def test_lab_sweep_band_absent_param_returns_empty(ap):
    d = ap.load_lab_defaults()
    band = ap.lab_sweep_band(d, "sbs", "TOTALLY_ABSENT_PARAM")
    assert band == []


def test_lab_sweep_band_absent_phase_and_param_returns_empty(ap):
    d = ap.load_lab_defaults()
    band = ap.lab_sweep_band(d, "cluster", "NONEXISTENT")
    assert band == []


def test_lab_sweep_spec_returns_dict_for_adaptive(ap):
    """THRESHOLD_READS / PEAK_WIDTH are adaptive dict specs in lab_defaults."""
    d = ap.load_lab_defaults()
    spec = ap.lab_sweep_spec(d, "sbs", "THRESHOLD_READS")
    assert isinstance(spec, dict)
    assert spec.get("coarse") and spec.get("refine_step") == 50
    pw = ap.lab_sweep_spec(d, "sbs", "PEAK_WIDTH")
    assert isinstance(pw, dict)
    assert pw.get("max") == 20


def test_lab_sweep_spec_absent_returns_none(ap):
    d = ap.load_lab_defaults()
    assert ap.lab_sweep_spec(d, "sbs", "NOPE") is None


def test_lab_sweep_band_returns_coarse_for_dict_spec(ap):
    """lab_sweep_band degrades a dict spec to its coarse list."""
    d = {"sweep_bands": {"sbs": {"X": {"coarse": [1, 2, 3], "refine_step": 1}}}}
    assert ap.lab_sweep_band(d, "sbs", "X") == [1, 2, 3]


def test_lab_sweep_band_still_handles_plain_list(ap):
    d = {"sweep_bands": {"sbs": {"X": [5, 6, 7]}}}
    assert ap.lab_sweep_band(d, "sbs", "X") == [5, 6, 7]


def test_load_lab_defaults_custom_path(ap, tmp_path):
    """Custom path with minimal YAML loads correctly."""
    import yaml
    custom = tmp_path / "custom_lab.yaml"
    custom.write_text(yaml.dump({
        "autonomy_mode": "unattended",
        "sweep_bands": {"sbs": {"MY_PARAM": [1, 2, 3]}},
        "sweep_tiles": {"n_tiles": 3},
        "recovery": {"max_attempts": 2},
    }))
    d = ap.load_lab_defaults(path=str(custom))
    assert d["autonomy_mode"] == "unattended"
    band = ap.lab_sweep_band(d, "sbs", "MY_PARAM")
    assert band == [1, 2, 3]


# ---------------------------------------------------------------------------
# T7: progress.json helpers
# ---------------------------------------------------------------------------


def test_progress_path_returns_expected_location(ap, tmp_path):
    p = ap.progress_path(tmp_path)
    assert p == tmp_path / ".brieflow" / "progress.json"


def test_phase_done_false_for_fresh_dir(ap, tmp_path):
    assert ap.phase_done(tmp_path, "sbs") is False


def test_mark_phase_done_creates_file(ap, tmp_path):
    ap.mark_phase_done(tmp_path, "preprocess")
    p = ap.progress_path(tmp_path)
    assert p.exists()


def test_mark_phase_done_phase_done_roundtrip(ap, tmp_path):
    ap.mark_phase_done(tmp_path, "sbs")
    assert ap.phase_done(tmp_path, "sbs") is True


def test_mark_phase_done_does_not_affect_other_phases(ap, tmp_path):
    ap.mark_phase_done(tmp_path, "preprocess")
    assert ap.phase_done(tmp_path, "sbs") is False
    assert ap.phase_done(tmp_path, "phenotype") is False


def test_mark_multiple_phases_done(ap, tmp_path):
    ap.mark_phase_done(tmp_path, "preprocess")
    ap.mark_phase_done(tmp_path, "sbs")
    ap.mark_phase_done(tmp_path, "phenotype")
    assert ap.phase_done(tmp_path, "preprocess") is True
    assert ap.phase_done(tmp_path, "sbs") is True
    assert ap.phase_done(tmp_path, "phenotype") is True
    assert ap.phase_done(tmp_path, "merge") is False


def test_set_paused_creates_paused_entry(ap, tmp_path):
    ap.set_paused(tmp_path, "sbs", "qc gate failed: single_map_rate=0.1")
    p = ap.progress_path(tmp_path)
    data = json.loads(p.read_text())
    assert "paused" in data
    assert data["paused"]["phase"] == "sbs"
    assert "qc gate failed" in data["paused"]["reason"]


def test_set_paused_overwrites_previous_paused(ap, tmp_path):
    ap.set_paused(tmp_path, "sbs", "first reason")
    ap.set_paused(tmp_path, "phenotype", "second reason")
    data = json.loads(ap.progress_path(tmp_path).read_text())
    assert data["paused"]["phase"] == "phenotype"
    assert data["paused"]["reason"] == "second reason"


def test_set_paused_preserves_done_phases(ap, tmp_path):
    ap.mark_phase_done(tmp_path, "preprocess")
    ap.set_paused(tmp_path, "sbs", "disk full")
    assert ap.phase_done(tmp_path, "preprocess") is True


def test_progress_atomic_write(ap, tmp_path):
    """Verify multiple sequential writes keep file consistent."""
    for phase in ["preprocess", "sbs", "phenotype"]:
        ap.mark_phase_done(tmp_path, phase)
    data = json.loads(ap.progress_path(tmp_path).read_text())
    assert set(data.get("done_phases", [])) >= {"preprocess", "sbs", "phenotype"}


# ---------------------------------------------------------------------------
# resolve_cluster_final: agent selects FINAL_LEIDEN_RESOLUTIONS from the
# notebook's resolution table and patches the marker.
# ---------------------------------------------------------------------------

_CLUSTER_LOG = """\
Creating clusters for resolution: 1
  resolution 1: 5 clusters, median genes/cluster 95.0
Creating clusters for resolution: 9
  resolution 9: 42 clusters, median genes/cluster 22.0
Creating clusters for resolution: 11
  resolution 11: 60 clusters, median genes/cluster 18.0
Creating clusters for resolution: 13
  resolution 13: 80 clusters, median genes/cluster 16.0
Creating clusters for resolution: 100
  resolution 100: 900 clusters, median genes/cluster 1.0
"""


def test_resolve_cluster_final_selects_and_patches(ap, tmp_path, monkeypatch):
    # Fake the cluster notebook run: write the resolution table to the log.
    log = tmp_path / "cluster.notebook.log"
    log.write_text(_CLUSTER_LOG)
    monkeypatch.setattr(ap, "run_phase_notebook",
                        lambda *a, **k: {"ok": True, "returncode": 0, "log": log})

    patched = {}
    monkeypatch.setattr(ap, "patch_marker_block_value",
                        lambda nb, var, val: patched.update({var: val}))
    monkeypatch.setattr(ap, "record_tuned_confirmed", lambda *a, **k: None)
    monkeypatch.setattr(ap, "save_interview", lambda *a, **k: None)

    lab = {"cluster_selection": {"target_genes_per_cluster": 20, "genes_per_cluster_tolerance": 5}}
    final = ap.resolve_cluster_final(tmp_path, "env", lab, {"params": []}, tmp_path / "iv.json")

    # in-band medians 9 (22), 11 (18), 13 (16); fine-swept to a step-1 range ±1 (f7795b7)
    assert final == [8, 9, 10, 11, 12, 13, 14]
    assert patched["FINAL_LEIDEN_RESOLUTIONS"] == repr([8, 9, 10, 11, 12, 13, 14])


def test_resolve_cluster_final_empty_table_returns_empty(ap, tmp_path, monkeypatch):
    log = tmp_path / "cluster.notebook.log"
    log.write_text("Creating clusters for resolution: 5\n(no parseable table)\n")
    monkeypatch.setattr(ap, "run_phase_notebook",
                        lambda *a, **k: {"ok": True, "returncode": 0, "log": log})
    called = {"patch": False}
    monkeypatch.setattr(ap, "patch_marker_block_value",
                        lambda *a, **k: called.update({"patch": True}))
    lab = {"cluster_selection": {"target_genes_per_cluster": 20, "genes_per_cluster_tolerance": 5}}
    final = ap.resolve_cluster_final(tmp_path, "env", lab, {"params": []}, tmp_path / "iv.json")
    assert final == []
    assert called["patch"] is False  # nothing to patch


# ---------------------------------------------------------------------------
# resolve_merge_initial_sbs_tiles: auto-probe well-spread anchors + patch.
# ---------------------------------------------------------------------------

def _merge_iv(value=None, approach="auto", positions=None):
    params = [
        {"notebook": "merge", "param_name": "initial_sbs_tiles", "value": value,
         "gating": {"param": "initial_sites_approach", "equals": "auto"}},
        {"notebook": "merge", "param_name": "initial_sites_approach", "value": approach},
    ]
    if positions is not None:
        approach = "positions" if positions else "fast"
        params.append({"notebook": "merge", "param_name": "MERGE_APPROACH", "value": approach})
    return {"params": params}


def test_resolve_merge_initial_sbs_tiles_patches(ap, tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "select_initial_sbs_tiles",
                        lambda *a, **k: {"tiles": [0, 2, 6, 8], "well": "A1", "rationale": "x"})
    patched = {}
    monkeypatch.setattr(ap, "patch_marker_block_value",
                        lambda nb, var, val: patched.update({var: val}))
    monkeypatch.setattr(ap, "record_tuned_confirmed", lambda *a, **k: None)
    monkeypatch.setattr(ap, "save_interview", lambda *a, **k: None)
    tiles = ap.resolve_merge_initial_sbs_tiles(tmp_path, {}, _merge_iv(), tmp_path / "iv.json")
    assert tiles == [0, 2, 6, 8]
    assert patched["INITIAL_SBS_TILES"] == repr([0, 2, 6, 8])


def test_resolve_merge_initial_sbs_tiles_respects_override(ap, tmp_path, monkeypatch):
    called = {"probe": False}
    def _probe(*a, **k):
        called["probe"] = True
        return {"tiles": [1]}
    monkeypatch.setattr(ap, "select_initial_sbs_tiles", _probe)
    tiles = ap.resolve_merge_initial_sbs_tiles(tmp_path, {}, _merge_iv(value=[5, 10, 15]), tmp_path / "iv.json")
    assert tiles == [5, 10, 15]
    assert called["probe"] is False  # operator value wins; no probe


def test_resolve_merge_initial_sbs_tiles_skips_positions_and_manual(ap, tmp_path, monkeypatch):
    called = {"probe": False}
    def _probe(*a, **k):
        called["probe"] = True
        return {"tiles": [1]}
    monkeypatch.setattr(ap, "select_initial_sbs_tiles", _probe)
    # positions on -> skip
    assert ap.resolve_merge_initial_sbs_tiles(tmp_path, {}, _merge_iv(positions=True), tmp_path / "iv.json") == []
    # manual approach -> row inactive -> skip
    assert ap.resolve_merge_initial_sbs_tiles(tmp_path, {}, _merge_iv(approach="manual"), tmp_path / "iv.json") == []
    assert called["probe"] is False


def test_merge_anchors_not_unresolved_on_positions(ap, tmp_path, monkeypatch):
    # A positions merge needs no INITIAL_SBS_TILES, even when a stale "auto" approach is set.
    monkeypatch.setattr(ap, "load_interview", lambda *_a, **_k: _merge_iv(positions=True))
    assert ap._merge_anchors_unresolved(tmp_path, "t") is False
    monkeypatch.setattr(ap, "load_interview", lambda *_a, **_k: _merge_iv(positions=False))
    assert ap._merge_anchors_unresolved(tmp_path, "t") is True
