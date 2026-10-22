"""Parallel tile sweeps: `adaptive_marimo_sweep_multitile` and
`joint_marimo_sweep_multitile` run tiles concurrently on up to
`max_parallel_kernels` live kernels. Thread-safe fake kernel seam keyed by port.
Locks: peak concurrency is bounded by the cap and actually >1 when allowed;
kernel bring-up is serialized (no two `start_marimo_kernel` calls overlap);
winners equal the sequential run; every started kernel is stopped.
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
import sys
import threading
import time
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


def _make_nb(path: Path) -> Path:
    nb = path / "sbs.py"
    nb.write_text(
        "# === OPERATOR PARAMETERS ===\nTEST_PLATE = 1\nTEST_WELL = 'A1'\nTEST_TILE = 1\n"
        "PEAK_WIDTH = 5\nTHRESHOLD_READS = 100\n# === END OPERATOR PARAMETERS ===\n"
    )
    return nb


SPEC_T = {"coarse": [100, 400, 1600], "refine_radius": 100, "refine_step": 50, "min": 25, "max": 6400}
SPECS = {"PEAK_WIDTH": {"coarse": [3, 7, 11], "refine_step": 1, "min": 3, "max": 20}, "THRESHOLD_READS": SPEC_T}
OPT_W, OPT_T = 7, 400
TILES = [(1, "A1", t) for t in range(1, 7)]


def _rate(w, t):
    return 0.5 - 0.02 * abs(w - OPT_W) - 0.1 * abs(math.log2(t / OPT_T))


class _Harness:
    """Per-port state so concurrent tiles don't share `outer`; records live-kernel
    count and start-call overlap under a lock."""

    def __init__(self, bs, monkeypatch, dwell_s=0.05):
        self.lock = threading.Lock()
        self.live = 0
        self.peak_live = 0
        self.in_start = 0
        self.start_overlap = False
        self.started: list = []
        self.stopped: list = []
        self.outer: dict = {}
        self.dwell = dwell_s
        self._next = 3000
        monkeypatch.setattr(bs, "start_marimo_kernel", self._start)
        monkeypatch.setattr(bs, "stop_marimo_kernel", self._stop)
        monkeypatch.setattr(bs, "_kernel_baseline", self._baseline)
        monkeypatch.setattr(bs, "_kernel_sweep", self._sweep)

    def _start(self, nb_copy, conda_env=None, watch=True, cwd=None, **kw):
        with self.lock:
            self.in_start += 1
            if self.in_start > 1:
                self.start_overlap = True
        time.sleep(self.dwell)
        with self.lock:
            self.in_start -= 1
            self._next += 1
            port = self._next
            self.started.append(port)
            self.live += 1
            self.peak_live = max(self.peak_live, self.live)
        return port, port, "s"

    def _stop(self, pid, timeout_s=5.0):
        with self.lock:
            self.live -= 1
            self.stopped.append(pid)

    def _baseline(self, port, prints_log, timeout_s):
        time.sleep(self.dwell)

    def _sweep(self, port, param, values, out_log, prints_log, timeout_s):
        values = list(values)
        time.sleep(self.dwell / 5)
        if param == "PEAK_WIDTH":
            with self.lock:
                self.outer[port] = values[0]
            return pd.DataFrame([{"threshold": values[0], "single_rate": None, "any_rate": None, "qc_pass": True, "elapsed_s": 0}])
        w = self.outer.get(port, OPT_W)
        return pd.DataFrame([{"threshold": t, "single_rate": _rate(w, t), "any_rate": None, "qc_pass": True, "elapsed_s": 0} for t in values])


def _run_adaptive(bs, nb, **kw):
    return asyncio.run(bs.adaptive_marimo_sweep_multitile(
        nb, "THRESHOLD_READS", SPEC_T, TILES, work_dir=nb.parent / "w", conda_env="env", **kw))


def _run_joint(bs, nb, **kw):
    return asyncio.run(bs.joint_marimo_sweep_multitile(
        nb, SPECS, TILES, work_dir=nb.parent / "j", conda_env="env", **kw))


def test_adaptive_runs_tiles_concurrently_within_cap(bs, monkeypatch, tmp_path):
    h = _Harness(bs, monkeypatch)
    nb = _make_nb(tmp_path)
    df = _run_adaptive(bs, nb, max_parallel_kernels=3)
    assert 1 < h.peak_live <= 3
    assert not h.start_overlap  # bring-up serialized
    assert sorted(h.stopped) == sorted(h.started) and len(h.started) == len(TILES)
    assert int(bs.pick_sweep_winner(df)) == OPT_T
    assert (df["n_tiles"] == len(TILES)).all()


def test_adaptive_sequential_when_cap_is_one(bs, monkeypatch, tmp_path):
    h = _Harness(bs, monkeypatch, dwell_s=0.01)
    nb = _make_nb(tmp_path)
    df = _run_adaptive(bs, nb, max_parallel_kernels=1)
    assert h.peak_live == 1 and int(bs.pick_sweep_winner(df)) == OPT_T


def test_joint_parallel_matches_sequential_winners(bs, monkeypatch, tmp_path):
    h = _Harness(bs, monkeypatch)
    nb = _make_nb(tmp_path)
    winners_par, summary_par = _run_joint(bs, nb, max_parallel_kernels=4)
    assert 1 < h.peak_live <= 4 and not h.start_overlap
    assert sorted(h.stopped) == sorted(h.started) and len(h.started) == len(TILES)
    h2 = _Harness(bs, monkeypatch, dwell_s=0.0)
    winners_seq, summary_seq = _run_joint(bs, nb, max_parallel_kernels=1)
    assert h2.peak_live == 1
    assert winners_par == winners_seq == {"PEAK_WIDTH": OPT_W, "THRESHOLD_READS": OPT_T}
    key = ["outer", "inner"]
    a = summary_par.sort_values(key).reset_index(drop=True)
    b = summary_seq.sort_values(key).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b, check_dtype=False)
    assert (a["n_tiles"] == len(TILES)).all()


def test_default_cap_is_lab_default(bs):
    assert bs.DEFAULT_MAX_PARALLEL_KERNELS == 3
