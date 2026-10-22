"""Stage 4 sweep defects from a 4-color SBS screen: kernel timeouts were reported as "no
finite mapping rate", the baseline limit ignored tile size, and each tile refined around its
own best value so refine rows were not comparable across tiles."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import brieflow_sweep as bs  # noqa: E402

pd = pytest.importorskip("pandas")

NB = (
    "# === OPERATOR PARAMETERS ===\nTEST_PLATE = 1\nTEST_WELL = 'A1'\nTEST_TILE = 1\n"
    "PEAK_WIDTH = 5\nTHRESHOLD_READS = 100\n# === END OPERATOR PARAMETERS ===\n"
)


class _Kernels:
    """Fake live kernels: per-tile optimum, optional baseline failure, records timeouts."""

    def __init__(self, monkeypatch, optimum=None, baseline_error=None):
        self.optimum = optimum or {}
        self.baseline_error = baseline_error
        self.baseline_limits = []
        self.sweeps = []
        self._tile = {}
        monkeypatch.setattr(bs, "start_marimo_kernel", self._start)
        monkeypatch.setattr(bs, "stop_marimo_kernel", lambda pid, timeout_s=5.0: None)
        monkeypatch.setattr(bs, "_kernel_baseline", self._baseline)
        monkeypatch.setattr(bs, "_kernel_sweep", self._sweep)

    def _start(self, nb_copy, conda_env=None, watch=True, cwd=None, **kw):
        port = 3000 + len(self._tile)
        self._tile[port] = Path(nb_copy).stem.split("_t")[-1]
        return port, port, "s"

    def _baseline(self, port, prints_log, timeout_s):
        self.baseline_limits.append(timeout_s)
        if self.baseline_error:
            raise RuntimeError(self.baseline_error)

    def _sweep(self, port, param, values, out_log, prints_log, timeout_s):
        tile = self._tile[port]
        self.sweeps.append((tile, param, list(values)))
        best = self.optimum.get(tile, 12)
        return pd.DataFrame([{"threshold": v, "single_rate": 1 - abs(v - best) / 100,
                              "any_rate": None, "qc_pass": True, "elapsed_s": 0} for v in values])


def _nb(tmp_path):
    (tmp_path / "sbs.py").write_text(NB)
    return tmp_path / "sbs.py"


def _adaptive(tmp_path, tiles, parallel):
    spec = {"coarse": [3, 7, 11, 15, 19], "refine_radius": 2, "refine_step": 1, "min": 3, "max": 20}
    return asyncio.run(bs.adaptive_marimo_sweep_multitile(
        _nb(tmp_path), "THRESHOLD", spec, tiles, work_dir=tmp_path / "w", conda_env="env",
        max_parallel_kernels=parallel))


def test_baseline_timeouts_are_reported_as_timeouts(tmp_path, monkeypatch):
    _Kernels(monkeypatch, baseline_error="in-kernel execution failed (rc=1): TimeoutError: "
             "SWEEP_TIMEOUT: baseline did not settle within 900 s")
    with pytest.raises(bs.SweepTimeout, match="timed out on 2/2 tile"):
        _adaptive(tmp_path, [(1, "A1", 1), (1, "A2", 2)], 2)


def test_other_failures_still_say_no_rate_with_their_kinds(tmp_path, monkeypatch):
    _Kernels(monkeypatch, baseline_error="in-kernel execution failed (rc=1): KeyError: 'x'")
    with pytest.raises(RuntimeError, match="error x2") as info:
        _adaptive(tmp_path, [(1, "A1", 1), (1, "A2", 2)], 2)
    assert not isinstance(info.value, bs.SweepTimeout)


def test_refine_points_are_shared_across_tiles(tmp_path, monkeypatch):
    k = _Kernels(monkeypatch, optimum={"1": 8, "2": 16})
    df = _adaptive(tmp_path, [(1, "A1", 1), (1, "A2", 2)], 2)
    fine = {tile: vals for tile, _p, vals in k.sweeps if vals != [3, 7, 11, 15, 19]}
    assert fine["1"] == fine["2"]  # one plan, from the cross-tile coarse winner
    assert set(df[df["value"].isin(fine["1"])]["n_tiles"]) == {2}


def test_unsettled_value_raises_instead_of_becoming_nan(monkeypatch):
    class Cell:
        code = "THRESHOLD = 1"

    class Ctx:
        cells = {"c1": Cell()}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def edit_cell(self, *a, **k):
            pass

        def run_cell(self, *a):
            pass

    monkeypatch.setattr(bs, "find_marker_cell", lambda ctx, name: "c1")
    monkeypatch.setattr(bs, "_await_kernel_settle", lambda f, timeout_s: False)
    with pytest.raises(bs.SweepTimeout, match="did not settle within 5 s"):
        asyncio.run(bs.run_marimo_sweep({"ctx_factory": Ctx}, "THRESHOLD", [2], timeout_s=5))


def test_baseline_limit_scales_with_tile_image_size(tmp_path):
    arr = tmp_path / "brieflow_output/preprocess/sbs/image_1.zarr/A/1/173"
    for cycle in range(1, 8):
        (arr / str(cycle) / "0").mkdir(parents=True)
        (arr / str(cycle) / "0" / "zarr.json").write_text(
            json.dumps({"node_type": "array", "shape": [1, 5, 1, 1480, 1480]}))
    mpx = bs.tile_work_mpx(tmp_path, 1, "A1", 173)
    assert mpx == pytest.approx(7 * 5 * 1480 * 1480 / 1e6)
    assert bs.baseline_timeout(900, mpx) == pytest.approx(
        bs.BASELINE_TIMEOUT_MARGIN * bs.BASELINE_S_PER_MPX * mpx)
    assert bs.baseline_timeout(900, 1.0) == 900 and bs.baseline_timeout(900, None) == 900


def test_joint_refine_bands_are_the_same_on_every_tile(tmp_path, monkeypatch):
    k = _Kernels(monkeypatch, optimum={"1": 400, "2": 800})
    specs = {"PEAK_WIDTH": {"coarse": [3, 7], "refine_step": 1, "min": 3, "max": 20},
             "THRESHOLD_READS": {"coarse": [100, 400, 800, 1600], "min": 25, "max": 6400}}
    winners, summary = asyncio.run(bs.joint_marimo_sweep_multitile(
        _nb(tmp_path), specs, [(1, "A1", 1), (1, "A2", 2)], work_dir=tmp_path / "w",
        conda_env="env", max_parallel_kernels=2))
    refine = {}
    for tile, param, vals in k.sweeps:
        if param == "THRESHOLD_READS" and vals != specs["THRESHOLD_READS"]["coarse"]:
            refine.setdefault(tile, []).append(vals)
    assert refine["1"] == refine["2"]  # each tile used to refine around its own optimum
    refined = summary[~summary["inner"].isin(specs["THRESHOLD_READS"]["coarse"])]
    assert not refined.empty and set(refined["n_tiles"]) == {2}
