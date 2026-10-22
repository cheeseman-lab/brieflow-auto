"""joint_marimo_sweep_multitile + its resolver wiring: two interacting marker params
(PEAK_WIDTH outer, THRESHOLD_READS inner) swept as a PAIR on one live kernel per tile.

Fakes the kernel seam (start/stop, _kernel_baseline, _kernel_sweep) exactly like
tests/test_marimo_sweep_multitile.py. Locks:
  * one bring-up + one baseline (segmentation) per tile, regardless of grid size;
  * the outer marker is set before EACH inner band (DAG order), so the inner band
    is evaluated under every outer value — a true product, not two 1-D passes;
  * the winner is the argmax over PAIRS of the cross-tile mean, including a
    local geometric refine that cannot beat a true interior optimum;
  * resolver: joint params are patched + tuned-confirmed from the joint result and
    SKIPPED by the sequential per-param loop; a failing joint route falls back.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
pd = pytest.importorskip("pandas")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bs():
    return _load("brieflow_sweep")


@pytest.fixture(scope="module")
def resolvers():
    return _load("brieflow_resolvers")


def _make_nb(path: Path) -> Path:
    nb = path / "sbs.py"
    nb.write_text(
        "# === OPERATOR PARAMETERS ===\nTEST_PLATE = 1\nTEST_WELL = 'A1'\nTEST_TILE = 1\n"
        "PEAK_WIDTH = 5\nTHRESHOLD_READS = 100\n# === END OPERATOR PARAMETERS ===\n"
    )
    return nb


SPECS = {
    "PEAK_WIDTH": {"coarse": [3, 7, 11], "refine_step": 1, "min": 3, "max": 20},
    "THRESHOLD_READS": {"coarse": [100, 400, 1600], "min": 25, "max": 6400},
}
# True optimum at (width 7, threshold 400); rate falls off in both directions.
OPT_W, OPT_T = 7, 400


def _rate(w, t):
    import math
    return 0.5 - 0.02 * abs(w - OPT_W) - 0.1 * abs(math.log2(t / OPT_T))


class _Harness:
    def __init__(self, bs, monkeypatch, *, fail_bringup_on=None, fail_band_outer=None):
        self.events: list = []
        self.stopped: list = []
        self.started: list = []
        self._pid = 100
        self._fail = fail_bringup_on
        self._fail_band_outer = fail_band_outer
        self._outer = None
        monkeypatch.setattr(bs, "start_marimo_kernel", self._start)
        monkeypatch.setattr(bs, "stop_marimo_kernel", self._stop)
        monkeypatch.setattr(bs, "_kernel_baseline", self._baseline)
        monkeypatch.setattr(bs, "_kernel_sweep", self._sweep)

    def _start(self, nb_copy, conda_env=None, watch=True, cwd=None, **kw):
        assert watch is False and cwd is not None
        tile = Path(nb_copy).stem
        if self._fail and self._fail in tile:
            raise RuntimeError("bring-up failed")
        self._pid += 1
        self.started.append(self._pid)
        self.events.append(("start", self._pid))
        return self._pid, 2718, "s"

    def _stop(self, pid, timeout_s=5.0):
        self.stopped.append(pid)
        self.events.append(("stop", pid))

    def _baseline(self, port, prints_log, timeout_s):
        self.events.append(("baseline",))

    def _sweep(self, port, param, values, out_log, prints_log, timeout_s):
        values = list(values)
        self.events.append(("sweep", param, values))
        if param == "PEAK_WIDTH":
            self._outer = values[0]
            if self._fail_band_outer is not None and values[0] == self._fail_band_outer:
                raise RuntimeError("in-kernel execution failed (rc=1): simulated")
            return pd.DataFrame([{"threshold": values[0], "single_rate": None, "any_rate": None, "qc_pass": True, "elapsed_s": 0}])
        return pd.DataFrame([{"threshold": t, "single_rate": _rate(self._outer, t), "any_rate": None,
                              "qc_pass": True, "elapsed_s": 0} for t in values])


def _run(bs, nb, tiles):
    # Sequential (shared-state harness); concurrency is covered by tests/test_sweep_parallel_tiles.py.
    return asyncio.run(bs.joint_marimo_sweep_multitile(
        nb, SPECS, tiles, work_dir=nb.parent / "w", conda_env="env", max_parallel_kernels=1))


def test_one_baseline_per_tile_and_true_product(bs, monkeypatch, tmp_path):
    h = _Harness(bs, monkeypatch)
    nb = _make_nb(tmp_path)
    winners, summary = _run(bs, nb, [(1, "A1", 1), (1, "A1", 2)])
    assert [e for e in h.events if e[0] == "baseline"] == [("baseline",)] * 2
    # every coarse outer value was set, and the full inner band swept under each
    coarse_outer_sets = [e for e in h.events if e[0] == "sweep" and e[1] == "PEAK_WIDTH" and e[2][0] in SPECS["PEAK_WIDTH"]["coarse"]]
    assert len(coarse_outer_sets) >= 2 * len(SPECS["PEAK_WIDTH"]["coarse"])
    inner_full = [e for e in h.events if e[0] == "sweep" and e[1] == "THRESHOLD_READS" and e[2] == SPECS["THRESHOLD_READS"]["coarse"]]
    assert len(inner_full) == 2 * len(SPECS["PEAK_WIDTH"]["coarse"])
    # DAG order: an outer set immediately precedes each full inner band
    ev = [e for e in h.events if e[0] == "sweep"]
    for i, e in enumerate(ev):
        if e[1] == "THRESHOLD_READS" and e[2] == SPECS["THRESHOLD_READS"]["coarse"]:
            assert ev[i - 1][1] == "PEAK_WIDTH"
    assert winners == {"PEAK_WIDTH": OPT_W, "THRESHOLD_READS": OPT_T}
    assert list(summary.columns) == ["outer", "inner", "mean_single_rate", "std_single_rate", "n_tiles", "qc_pass_frac"]
    assert sorted(h.stopped) == sorted(h.started)


def test_refine_probes_geometric_and_neighbour_widths(bs, monkeypatch, tmp_path):
    h = _Harness(bs, monkeypatch)
    nb = _make_nb(tmp_path)
    _run(bs, nb, [(1, "A1", 1)])
    inner_refine = [e[2] for e in h.events if e[0] == "sweep" and e[1] == "THRESHOLD_READS" and e[2] != SPECS["THRESHOLD_READS"]["coarse"] and len(e[2]) > 1]
    assert inner_refine and set(inner_refine[0]) == {283, 336, 476, 566}  # 400·2^(±1/2, ±1/4)
    outer_probes = {e[2][0] for e in h.events if e[0] == "sweep" and e[1] == "PEAK_WIDTH"}
    assert {6, 8} <= outer_probes  # OPT_W ± refine_step, not already in the coarse band


def test_bringup_failure_isolated_and_all_fail_raises(bs, monkeypatch, tmp_path):
    h = _Harness(bs, monkeypatch, fail_bringup_on="_t1")
    nb = _make_nb(tmp_path)
    winners, _ = _run(bs, nb, [(1, "A1", 1), (1, "A1", 2)])
    assert winners == {"PEAK_WIDTH": OPT_W, "THRESHOLD_READS": OPT_T} and len(h.started) == 1
    _Harness(bs, monkeypatch, fail_bringup_on="joint_p")
    with pytest.raises(RuntimeError, match="live-kernel route unavailable"):
        _run(bs, nb, [(1, "A1", 1)])


def test_cross_tile_winner_ignores_single_tile_refine_outlier(bs):
    """A real run: a refine point one lucky tile probed (12/800 → 0.605 on
    ONE tile) beat the 8-tile coarse optimum (15/800 → 0.542). With a majority
    requirement the grid pair wins; per-tile picks (min_tiles=1) still see it."""
    rows = []
    for t in range(8):
        rows += [{"tile": t, "outer": 15, "inner": 800, "single_rate": 0.54},
                 {"tile": t, "outer": 13, "inner": 800, "single_rate": 0.53}]
    rows.append({"tile": 3, "outer": 12, "inner": 800, "single_rate": 0.605})
    assert bs._pick_joint_winner(rows, min_tiles=4) == (15, 800)
    assert bs._pick_joint_winner(rows) == (12, 800)
    assert bs._pick_joint_winner(rows, min_tiles=50) == (15, 800)  # nothing qualifies → best-covered pairs


def test_band_failure_isolated_and_logged(bs, monkeypatch, tmp_path):
    """A failing band (e.g. the kernel choking after a zero-read point) must not
    discard the tile: the other bands still count, the winner is still found,
    and the traceback lands in <slug>.error.log instead of being swallowed."""
    h = _Harness(bs, monkeypatch, fail_band_outer=11)
    nb = _make_nb(tmp_path)
    winners, summary = _run(bs, nb, [(1, "A1", 1)])
    assert winners == {"PEAK_WIDTH": OPT_W, "THRESHOLD_READS": OPT_T}
    assert 11 not in set(summary["outer"])  # the failed band contributed no rows
    assert {3, 7} <= set(summary["outer"])  # the bands before/after it did
    err = nb.parent / "w" / "joint_p1_wA1_t1.error.log"
    assert err.exists() and "band o11" in err.read_text() and "simulated" in err.read_text()
    assert sorted(h.stopped) == sorted(h.started)


# --- resolver wiring ---------------------------------------------------------
def _ctx(tmp_path, joint_fn, adaptive_calls):
    nb = tmp_path / "sbs.py"
    nb.write_text("x = 1\n")
    patched: list = []
    recorded: list = []
    notes: list = []

    async def _adaptive(nb_path, param, spec, tiles, work_dir=None, conda_env=None, **kw):
        adaptive_calls.append(param)
        return pd.DataFrame([{"value": 5, "mean_single_rate": 0.1}])

    return dict(
        analysis_dir=tmp_path, conda_env="env",
        lab={"sweep_bands": {"sbs": {"PEAK_WIDTH": SPECS["PEAK_WIDTH"], "THRESHOLD_READS": SPECS["THRESHOLD_READS"]}},
             "sweep_joint": {"sbs": ["PEAK_WIDTH", "THRESHOLD_READS"]}, "sweep_tiles": {"n_tiles": 2}},
        iv={"params": []}, iv_path=tmp_path / "iv.json", nb_path=nb, tag="t",
        notify=lambda *a, **k: notes.append(a[2] if len(a) > 2 else a),
        select_validation_tiles_from_metadata=lambda *a, **k: {"tiles": [(1, "A1", 1)]},
        list_wells_from_metadata=lambda *a, **k: ["A1"], is_active=lambda r, iv: True,
        patch_marker_block_value=lambda nb, p, v: patched.append((p, v)),
        lab_sweep_spec=lambda lab, ph, p: lab["sweep_bands"][ph][p],
        lab_sweep_band=lambda lab, ph, p: list(lab["sweep_bands"][ph][p].get("coarse", [])),
        adaptive_sweep_headless=_adaptive, adaptive_marimo_sweep_multitile=_adaptive, sweep_headless=None,
        joint_marimo_sweep_multitile=joint_fn, pick_sweep_winner=lambda df: 5,
        record_tuned_confirmed=lambda iv, p, w, notebook=None: recorded.append((p, w)),
        save_interview=lambda iv, path: None, resolve_cluster_final=lambda *a: [5],
        resolve_merge_initial_sbs_tiles=lambda *a: [1], resolve_merge_det_range=lambda *a: [1, 2],
        _patched=patched, _recorded=recorded, _notes=notes,
    )


def test_resolver_uses_joint_and_skips_sequential(resolvers, tmp_path):
    calls: list = []

    async def _joint(nb_path, specs, tiles, work_dir=None, conda_env=None, max_parallel_kernels=None, **kw):
        assert list(specs) == ["PEAK_WIDTH", "THRESHOLD_READS"]
        assert max_parallel_kernels == 3  # lab default threaded through by the resolver
        return {"PEAK_WIDTH": 7, "THRESHOLD_READS": 400}, pd.DataFrame([{"outer": 7, "inner": 400}])

    ctx = _ctx(tmp_path, _joint, calls)
    resolvers.resolve_phase("sbs", ctx)
    assert ("PEAK_WIDTH", "7") in ctx["_patched"] and ("THRESHOLD_READS", "400") in ctx["_patched"]
    assert set(ctx["_recorded"]) == {("PEAK_WIDTH", 7), ("THRESHOLD_READS", 400)}
    assert calls == []  # sequential per-param sweeps were skipped for joint params


def test_resolver_falls_back_to_sequential_when_joint_fails(resolvers, tmp_path):
    calls: list = []

    async def _joint(*a, **k):
        raise RuntimeError("no kernels")

    ctx = _ctx(tmp_path, _joint, calls)
    resolvers.resolve_phase("sbs", ctx)
    assert sorted(calls) == ["PEAK_WIDTH", "THRESHOLD_READS"]
    assert any("falling back to sequential" in str(n) for n in ctx["_notes"])
