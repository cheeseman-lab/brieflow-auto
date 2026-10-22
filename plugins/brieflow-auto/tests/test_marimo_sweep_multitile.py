"""adaptive_marimo_sweep_multitile: the reactive (live-kernel) coarse-to-fine
multi-tile sweep (Fix B, v0.5.4).

The context-bound work (tees, baseline run, `run_marimo_sweep`) is injected INTO
the kernel through the marimo-pair bridge (`_execute_in_kernel`), because
`marimo._code_mode.get_context()` only attaches from inside the kernel. These
tests fake the kernel seam (`start_marimo_kernel`, `stop_marimo_kernel`,
`_kernel_baseline`, `_kernel_sweep`) so no real `marimo edit` server is needed —
the live-kernel contract itself is proven by tests/test_marimo_live.py. What we
lock here is the orchestrator's control flow:

  * the kernel is brought up and baselined (segmented) EXACTLY ONCE per tile;
  * coarse values then the fine band both sweep on that same live kernel;
  * `stop_marimo_kernel` runs for every started tile on success AND on a
    tile-level sweep exception (try/finally);
  * the aggregate DataFrame has the exact `sweep_headless` columns and
    `pick_sweep_winner` selects the max-mean value;
  * a tile whose kernel bring-up raises is skipped, survivors still aggregate;
  * no usable result (nothing came up, or kernels up but nothing executed)
    RAISES so the resolver falls back to the headless path.

Plus the bridge helpers themselves: `_kernel_sweep` reads the per-band TSV
back, the OK-marker guards raise, and `_execute_in_kernel` raises on a
non-zero bridge exit.
"""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
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


# A trivial notebook with the tile markers the orchestrator bakes into each copy
# (the copy+patch runs for real; only the kernel seam is faked).
def _make_nb(path: Path) -> Path:
    nb = path / "reactive_sbs.py"
    nb.write_text(
        """\
# === OPERATOR PARAMETERS ===
TEST_PLATE = 1
TEST_WELL = 'A1'
TEST_TILE = 1
THRESHOLD = 3
# === END OPERATOR PARAMETERS ===

print(f"SINGLE_MAP_RATE: {THRESHOLD / 100.0}")
"""
    )
    return nb


_SPEC = {
    "coarse": [3, 7, 11, 15, 19],
    "refine_radius": 2,
    "refine_step": 1,
    "min": 3,
    "max": 20,
}
# refine around coarse winner 11 (single_rate peaks nearest 12) → [9, 10, 12, 13].
_EXPECTED_FINE = [9, 10, 12, 13]


class _Harness:
    """Installs a fake kernel seam on the brieflow_sweep module and records an
    event log so the control flow can be asserted."""

    def __init__(self, bs, monkeypatch, *, fail_bringup_on=None, raise_sweep_on=None):
        self.bs = bs
        self.events: list = []
        self.stopped: list = []
        self.started_pids: list = []
        self._next_pid = 100
        self._fail_bringup_on = fail_bringup_on  # (plate, well, tile) or None
        self._raise_sweep_on = raise_sweep_on
        self._cur_tile = None

        monkeypatch.setattr(bs, "start_marimo_kernel", self._start)
        monkeypatch.setattr(bs, "stop_marimo_kernel", self._stop)
        monkeypatch.setattr(bs, "_kernel_baseline", self._baseline)
        monkeypatch.setattr(bs, "_kernel_sweep", self._sweep)

    def _tile_of(self, nb_copy: Path):
        # The copy name encodes p{plate}_w{well}_t{tile}; recover the tuple.
        stem = Path(nb_copy).stem
        _, _, rest = stem.partition("_p")
        plate, _, rest = rest.partition("_w")
        well, _, tile = rest.partition("_t")
        return (int(plate), well, int(tile))

    def _start(self, nb_copy, conda_env=None, watch=True, cwd=None, **kw):
        assert watch is False, "sweep kernels must start with watch=False"
        # The copy lives under work_dir, but the notebook resolves config/ and
        # brieflow_output/ relative to cwd — so cwd must be the ANALYSIS dir
        # (the original notebook's parent), never the copy's work_dir.
        # `_run` places copies at <analysis_dir>/work/<slug>.py.
        analysis_dir = Path(nb_copy).resolve().parent.parent
        assert cwd is not None, "sweep kernels must be started with an explicit cwd"
        assert Path(cwd).resolve() == analysis_dir, (cwd, analysis_dir)
        assert Path(cwd).resolve() != Path(nb_copy).resolve().parent, (
            "cwd must not be the copy's work_dir"
        )
        tile = self._tile_of(nb_copy)
        if self._fail_bringup_on is not None and tile == self._fail_bringup_on:
            raise RuntimeError(f"bring-up failed for {tile}")
        self._cur_tile = tile
        pid = self._next_pid
        self._next_pid += 1
        self.started_pids.append(pid)
        self.events.append(("start", pid, tile))
        return pid, 2718, "sess"

    def _stop(self, pid, timeout_s=5.0):
        self.events.append(("stop", pid))
        self.stopped.append(pid)

    def _baseline(self, port, prints_log, timeout_s):
        self.events.append(("baseline", self._cur_tile))

    def _sweep(self, port, param_name, values, out_log, prints_log, timeout_s):
        values = list(values)
        tile = self._cur_tile
        self.events.append(("sweep", tile, list(values)))
        if self._raise_sweep_on is not None and tile == self._raise_sweep_on:
            raise RuntimeError(f"sweep blew up for {tile}")
        # single_rate peaks at 12 → coarse winner 11, global optimum lands in fine.
        rows = [
            {
                "threshold": v,
                "single_rate": 1.0 - abs(v - 12) / 100.0,
                "any_rate": None,
                "qc_pass": True,
                "elapsed_s": 0.1,
            }
            for v in values
        ]
        return pd.DataFrame(rows)


def _run(bs, nb, tiles, **kw):
    # Sequential: these tests assert per-tile event ORDER through a shared
    # harness; concurrency is covered by tests/test_sweep_parallel_tiles.py.
    kw.setdefault("max_parallel_kernels", 1)
    return asyncio.run(
        bs.adaptive_marimo_sweep_multitile(
            nb, "THRESHOLD", _SPEC, tiles, work_dir=nb.parent / "work", conda_env="env", **kw
        )
    )


# --- orchestrator control flow ----------------------------------------------
def test_one_bringup_and_baseline_per_tile_coarse_then_fine(bs, monkeypatch, tmp_path):
    """Kernel comes up + is baselined (segmented) once per tile; coarse then
    fine sweep that same kernel."""
    h = _Harness(bs, monkeypatch)
    nb = _make_nb(tmp_path)
    tiles = [(1, "A1", 1), (1, "A1", 2)]
    _run(bs, nb, tiles)

    starts = [e for e in h.events if e[0] == "start"]
    assert len(starts) == 2  # exactly one bring-up per tile, not per value

    # Per tile the order is start → baseline → sweep(coarse) → sweep(fine) →
    # stop, with no second start/baseline interleaved (same live kernel, one
    # segmentation, across both bands).
    per_tile = {}
    cur = None
    for e in h.events:
        if e[0] == "start":
            cur = e[2]
            per_tile[cur] = []
        else:
            per_tile[cur].append(e)
    for tile, evs in per_tile.items():
        kinds = [x[0] for x in evs]
        assert kinds == ["baseline", "sweep", "sweep", "stop"], (tile, kinds)
        assert evs[1][2] == _SPEC["coarse"]      # coarse band first
        assert evs[2][2] == _EXPECTED_FINE       # fine band around coarse winner


def test_aggregate_shape_and_winner(bs, monkeypatch, tmp_path):
    """Aggregate columns match sweep_headless; pick_sweep_winner picks max mean."""
    _Harness(bs, monkeypatch)
    nb = _make_nb(tmp_path)
    tiles = [(1, "A1", 1), (1, "A1", 2)]
    df = _run(bs, nb, tiles)

    assert list(df.columns) == [
        "value", "mean_single_rate", "std_single_rate", "n_tiles", "qc_pass_frac"
    ]
    # Value 12 (only in the fine band) is the global single_rate optimum.
    assert bs.pick_sweep_winner(df) == 12
    row12 = df[df["value"] == 12].iloc[0]
    assert row12["n_tiles"] == 2          # both tiles tested the fine winner
    assert row12["qc_pass_frac"] == 1.0
    assert row12["mean_single_rate"] == pytest.approx(1.0)


def test_stop_called_for_every_tile_on_success(bs, monkeypatch, tmp_path):
    """Every started kernel is stopped on the happy path."""
    h = _Harness(bs, monkeypatch)
    nb = _make_nb(tmp_path)
    tiles = [(1, "A1", 1), (1, "A1", 2), (1, "A1", 3)]
    _run(bs, nb, tiles)
    assert sorted(h.stopped) == sorted(h.started_pids)
    assert len(h.stopped) == 3


def test_stop_called_when_tile_sweep_raises(bs, monkeypatch, tmp_path):
    """A tile whose in-kernel sweep raises still gets its kernel stopped, and
    the surviving tile still aggregates."""
    h = _Harness(bs, monkeypatch, raise_sweep_on=(1, "A1", 1))
    nb = _make_nb(tmp_path)
    tiles = [(1, "A1", 1), (1, "A1", 2)]
    df = _run(bs, nb, tiles)

    # Both kernels were brought up and both were stopped (try/finally).
    assert len(h.started_pids) == 2
    assert sorted(h.stopped) == sorted(h.started_pids)
    # Only the surviving tile contributed rows, but the aggregate is still valid.
    assert not df.empty
    assert bs.pick_sweep_winner(df) == 12


def test_bringup_failure_is_isolated(bs, monkeypatch, tmp_path):
    """A tile whose kernel bring-up raises is skipped (no stop for it); survivors
    still aggregate."""
    h = _Harness(bs, monkeypatch, fail_bringup_on=(1, "A1", 1))
    nb = _make_nb(tmp_path)
    tiles = [(1, "A1", 1), (1, "A1", 2)]
    df = _run(bs, nb, tiles)

    # Tile 1 never started → never stopped; tile 2 started and stopped.
    assert len(h.started_pids) == 1
    assert h.stopped == h.started_pids
    assert not df.empty
    assert bs.pick_sweep_winner(df) == 12


def test_all_bringups_fail_raises(bs, monkeypatch, tmp_path):
    """If tiles were supplied but no kernel comes up, raise so the resolver can
    fall back to the headless path."""
    nb = _make_nb(tmp_path)

    def _always_fail(nb_copy, conda_env=None, watch=True, **kw):
        raise RuntimeError("no kernel here")

    monkeypatch.setattr(bs, "start_marimo_kernel", _always_fail)
    with pytest.raises(RuntimeError, match="live-kernel route unavailable"):
        _run(bs, nb, [(1, "A1", 1), (1, "A1", 2)])


def test_kernel_starts_but_sweep_fails_raises_for_fallback(bs, monkeypatch, tmp_path):
    """Kernels come up but every in-kernel step raises (how an attach/bridge
    failure manifests) → no usable result → RAISE so the resolver falls back to
    the headless path, instead of returning empty (which would silently keep
    the default value). The kernel is still stopped via the finally."""
    h = _Harness(bs, monkeypatch, raise_sweep_on=(1, "A1", 1))
    nb = _make_nb(tmp_path)
    with pytest.raises(RuntimeError, match="live-kernel route unavailable"):
        _run(bs, nb, [(1, "A1", 1)])
    assert h.started_pids == h.stopped  # finally still stopped the started kernel


def test_empty_tiles_returns_canonical_columns(bs, monkeypatch, tmp_path):
    """No tiles → empty frame with the sweep_headless column set (no raise)."""
    _Harness(bs, monkeypatch)
    nb = _make_nb(tmp_path)
    df = _run(bs, nb, [])
    assert list(df.columns) == [
        "value", "mean_single_rate", "std_single_rate", "n_tiles", "qc_pass_frac"
    ]
    assert len(df) == 0


# --- the bridge seam ---------------------------------------------------------
def test_kernel_sweep_runs_in_kernel_and_reads_tsv_back(bs, monkeypatch, tmp_path):
    """`_kernel_sweep` injects run_marimo_sweep INTO the kernel (in-kernel
    ctx_factory, not an in-process one) and reads the TSV it writes back."""
    seen: dict = {}

    def _fake_exec(port, code, timeout_s, ok_marker=None):
        seen["port"] = port
        seen["code"] = code
        # Emulate run_marimo_sweep's append-mode TSV, incl. a None → "" rate.
        (tmp_path / "band.tsv").write_text(
            "threshold\tsingle_rate\tany_rate\tqc_pass\telapsed_s\n"
            "3\t0.91\t\tTrue\t0.1\n"
            "7\t\t\tFalse\t0.1\n"
        )
        return "SWEEP_OK 2\n"

    monkeypatch.setattr(bs, "_execute_in_kernel", _fake_exec)
    import numpy as np

    df = bs._kernel_sweep(
        2718, "THRESHOLD", [np.int64(3), 7], tmp_path / "band.tsv", tmp_path / "p.log", 60.0
    )
    assert seen["port"] == 2718
    # The context is obtained in-kernel and the production sweep fn is driven.
    assert "cm.get_context" in seen["code"]
    assert "run_marimo_sweep" in seen["code"]
    assert "param_name='THRESHOLD'" in seen["code"]
    assert "values=[3, 7]" in seen["code"]        # numpy scalar coerced to native
    assert list(df["threshold"]) == [3, 7]
    assert df["single_rate"].iloc[0] == pytest.approx(0.91)
    assert pd.isna(df["single_rate"].iloc[1])
    assert bool(df["qc_pass"].iloc[0]) is True


def test_kernel_sweep_fresh_tsv_per_band(bs, monkeypatch, tmp_path):
    """A stale TSV from an earlier band is removed before the sweep so rows
    never bleed across bands."""
    tsv = tmp_path / "band.tsv"
    tsv.write_text("threshold\tsingle_rate\tany_rate\tqc_pass\telapsed_s\n99\t0.5\t\tTrue\t1\n")

    def _fake_exec(port, code, timeout_s, ok_marker=None):
        assert not tsv.exists(), "stale band TSV must be cleared before sweeping"
        return "SWEEP_OK 0\n"

    monkeypatch.setattr(bs, "_execute_in_kernel", _fake_exec)
    df = bs._kernel_sweep(1, "THRESHOLD", [1], tsv, tmp_path / "p.log", 60.0)
    assert df.empty
    assert list(df.columns) == ["threshold", "single_rate", "any_rate", "qc_pass", "elapsed_s"]


def test_kernel_sweep_raises_without_ok_marker(bs, monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "_execute_in_kernel", lambda port, code, timeout_s, ok_marker=None: "")
    with pytest.raises(RuntimeError, match="did not complete"):
        bs._kernel_sweep(1, "THRESHOLD", [1], tmp_path / "b.tsv", tmp_path / "p.log", 60.0)


def test_kernel_baseline_runs_all_cells_with_tees(bs, monkeypatch, tmp_path):
    seen: dict = {}

    def _fake_exec(port, code, timeout_s, ok_marker=None):
        seen["code"] = code
        return "BASELINE_OK 44\n"

    monkeypatch.setattr(bs, "_execute_in_kernel", _fake_exec)
    bs._kernel_baseline(1, tmp_path / "p.log", 60.0)
    assert "setup_kernel_tees" in seen["code"]
    assert "run_cell(cid)" in seen["code"]          # every cell, kernel boots stale
    assert "_await_kernel_settle" in seen["code"]


def test_kernel_baseline_raises_without_ok_marker(bs, monkeypatch, tmp_path):
    monkeypatch.setattr(bs, "_execute_in_kernel", lambda port, code, timeout_s, ok_marker=None: "nope")
    with pytest.raises(RuntimeError, match="baseline run did not complete"):
        bs._kernel_baseline(1, tmp_path / "p.log", 60.0)


def test_execute_in_kernel_accepts_nonzero_rc_when_marker_present(bs, monkeypatch):
    """The bridge exits 1 whenever ANY notebook cell raised during the execution
    (e.g. a zero-read value makes call_cells raise), but the injected sweep code
    completed and printed its marker: that is data, not a failure."""
    def _fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="SWEEP_OK 7\n", stderr="cell 'dGlV' raised KeyError")

    monkeypatch.setattr(bs.subprocess, "run", _fake_run)
    assert "SWEEP_OK" in bs._execute_in_kernel(2718, "x", 10.0, ok_marker="SWEEP_OK")
    with pytest.raises(RuntimeError, match="in-kernel execution failed"):
        bs._execute_in_kernel(2718, "x", 10.0)  # no marker → non-zero rc is a failure


def test_execute_in_kernel_retries_transient_no_session(bs, monkeypatch):
    """A session still attaching ("No active sessions") is transient: retry,
    don't fail the tile. Three kernels booting at once raced the first call."""
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(1)
        if len(calls) < 3:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No active sessions on the server.")
        return subprocess.CompletedProcess(cmd, 0, stdout="BASELINE_OK 44\n", stderr="")

    monkeypatch.setattr(bs.subprocess, "run", _fake_run)
    monkeypatch.setattr(bs, "_time", None, raising=False)
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    assert "BASELINE_OK" in bs._execute_in_kernel(2718, "x", 10.0, ok_marker="BASELINE_OK") and len(calls) == 3


def test_session_registered_false_on_closed_port():
    import importlib.util
    spec = importlib.util.spec_from_file_location("brieflow_marimo_t", SCRIPTS / "brieflow_marimo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._session_registered(1) is False  # nothing listens on port 1


def test_execute_in_kernel_raises_on_bridge_failure(bs, monkeypatch):
    def _fake_run(cmd, **kw):
        # addressed by URL, never by registry discovery (shared-$HOME race)
        assert "--url" in cmd and "http://127.0.0.1:2718" in cmd and "--port" not in cmd
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No running marimo instances found.")

    monkeypatch.setattr(bs.subprocess, "run", _fake_run)
    with pytest.raises(RuntimeError, match="No running marimo instances"):
        bs._execute_in_kernel(2718, "print(1)", 10.0)
