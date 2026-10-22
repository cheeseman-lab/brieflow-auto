"""Tests for _fan_out_notebook_runs, sweep_headless, and pick_sweep_winner.

Task 3: regression test that validate_tiles_headless keeps its exact DataFrame
shape after the _fan_out_notebook_runs refactor, plus a direct test of the
primitive against a trivial .py fixture.

Task 4: sweep_headless + pick_sweep_winner using a parameterized trivial
notebook fixture where different THRESHOLD values yield different mapping rates.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_wizard.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
# A brieflow screen env the fan-out tests run notebooks in (needs marimo).
SCREEN_ENV = os.environ.get("BRIEFLOW_TEST_CONDA_ENV", "brieflow_zarr3")


def _env_exists(name: str) -> bool:
    conda = shutil.which("conda")
    if not conda:
        return False
    out = subprocess.run([conda, "env", "list"], capture_output=True, text=True).stdout
    return any(line.split()[:1] == [name] for line in out.splitlines())


needs_screen_env = pytest.mark.skipif(
    not _env_exists(SCREEN_ENV), reason=f"conda env {SCREEN_ENV} (BRIEFLOW_TEST_CONDA_ENV) not found"
)


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Trivial notebook fixtures
# ---------------------------------------------------------------------------


def _make_trivial_nb(path: Path, single_rate: float = 0.6) -> Path:
    """Create a plain-Python notebook that prints parseable QC/mapping lines."""
    nb_file = path / "trivial_sbs.py"
    nb_file.write_text(
        f"""\
# === OPERATOR PARAMETERS ===
TEST_PLATE = 1
TEST_WELL = 'A1'
TEST_TILE = 1
# === END OPERATOR PARAMETERS ===

print(f"SINGLE_MAP_RATE: {single_rate}")
print(f"ANY_MAP_RATE: {single_rate + 0.05}")
"""
    )
    return nb_file


def _make_parameterized_nb(path: Path) -> Path:
    """Notebook where THRESHOLD controls the printed SINGLE_MAP_RATE.

    THRESHOLD=50  -> prints 0.50
    THRESHOLD=80  -> prints 0.80
    THRESHOLD=60  -> prints 0.60
    Pattern: rate = THRESHOLD / 100
    """
    nb_file = path / "sweep_sbs.py"
    nb_file.write_text(
        """\
# === OPERATOR PARAMETERS ===
TEST_PLATE = 1
TEST_WELL = 'A1'
TEST_TILE = 1
THRESHOLD = 50
# === END OPERATOR PARAMETERS ===

rate = THRESHOLD / 100.0
print(f"SINGLE_MAP_RATE: {rate}")
print(f"ANY_MAP_RATE: {rate + 0.05}")
"""
    )
    return nb_file


# ---------------------------------------------------------------------------
# Task 3 — _fan_out_notebook_runs
# ---------------------------------------------------------------------------


@needs_screen_env
def test_fan_out_notebook_runs_returns_list_of_dicts(bw, tmp_path):
    """_fan_out_notebook_runs runs a trivial notebook and returns parseable rows."""
    nb = _make_trivial_nb(tmp_path, single_rate=0.6)
    jobs = [
        {
            "nb_path": nb,
            "patches": {
                "TEST_PLATE": repr(1),
                "TEST_WELL": repr("A1"),
                "TEST_TILE": repr(1),
            },
            "label": {"plate": 1, "well": "A1", "tile": 1},
        }
    ]
    rows = asyncio.run(
        bw._fan_out_notebook_runs(
            jobs,
            work_dir=tmp_path / "work",
            conda_env=SCREEN_ENV,
            omp_threads=1,
            max_parallel=2,
            timeout_s=60.0,
        )
    )
    assert isinstance(rows, list)
    assert len(rows) == 1
    row = rows[0]
    assert set(row["label"].items()) == {("plate", 1), ("well", "A1"), ("tile", 1)}
    # The trivial notebook prints SINGLE_MAP_RATE: 0.6
    assert row["single_rate"] == pytest.approx(0.6, abs=1e-6)
    assert row["exit_code"] == 0
    assert row["error"] is None


@needs_screen_env
def test_fan_out_notebook_runs_parallel_bounded(bw, tmp_path):
    """Multiple jobs run concurrently but are bounded by max_parallel."""
    nb = _make_trivial_nb(tmp_path, single_rate=0.7)
    jobs = [
        {
            "nb_path": nb,
            "patches": {
                "TEST_PLATE": repr(1),
                "TEST_WELL": repr("A1"),
                "TEST_TILE": repr(i),
            },
            "label": {"plate": 1, "well": "A1", "tile": i},
        }
        for i in range(3)
    ]
    rows = asyncio.run(
        bw._fan_out_notebook_runs(
            jobs,
            work_dir=tmp_path / "work",
            conda_env=SCREEN_ENV,
            omp_threads=1,
            max_parallel=2,
            timeout_s=60.0,
        )
    )
    assert len(rows) == 3
    for row in rows:
        assert row["exit_code"] == 0
        assert row["single_rate"] == pytest.approx(0.7, abs=1e-6)


@needs_screen_env
def test_fan_out_notebook_runs_patch_failure_is_row_error(bw, tmp_path):
    """A bad patch (missing var) surfaces as error in the row, not an exception."""
    nb = _make_trivial_nb(tmp_path, single_rate=0.5)
    jobs = [
        {
            "nb_path": nb,
            "patches": {
                "NONEXISTENT_VAR": repr(999),
                "TEST_PLATE": repr(1),
                "TEST_WELL": repr("A1"),
                "TEST_TILE": repr(1),
            },
            "label": {"plate": 1, "well": "A1", "tile": 1},
        }
    ]
    rows = asyncio.run(
        bw._fan_out_notebook_runs(
            jobs,
            work_dir=tmp_path / "work",
            conda_env=SCREEN_ENV,
            omp_threads=1,
            max_parallel=2,
            timeout_s=60.0,
        )
    )
    assert len(rows) == 1
    assert rows[0]["error"] is not None
    assert "patch failed" in rows[0]["error"]


# ---------------------------------------------------------------------------
# Task 3 — validate_tiles_headless regression (shape must be identical)
# ---------------------------------------------------------------------------

EXPECTED_VALIDATE_COLUMNS = {
    "plate", "well", "tile", "single_rate", "any_rate",
    "qc_pass", "qc_failed_keys", "elapsed_s", "exit_code", "error", "log_fp",
}


@needs_screen_env
def test_validate_tiles_headless_empty_tiles_returns_correct_columns(bw, tmp_path):
    """Empty tiles list returns a DataFrame with the canonical columns."""
    nb = _make_trivial_nb(tmp_path)
    df = asyncio.run(
        bw.validate_tiles_headless(
            nb_path=nb,
            winners={},
            tiles=[],
            work_dir=tmp_path / "work",
            conda_env=SCREEN_ENV,
        )
    )
    assert set(df.columns) == EXPECTED_VALIDATE_COLUMNS
    assert len(df) == 0


@needs_screen_env
def test_validate_tiles_headless_single_tile_shape(bw, tmp_path):
    """After refactor, validate_tiles_headless returns the same column set per row."""
    nb = _make_trivial_nb(tmp_path, single_rate=0.65)
    df = asyncio.run(
        bw.validate_tiles_headless(
            nb_path=nb,
            winners={},
            tiles=[(1, "A1", 1)],
            work_dir=tmp_path / "work",
            conda_env=SCREEN_ENV,
            omp_threads=1,
            max_parallel=2,
            timeout_s=60.0,
        )
    )
    assert set(df.columns) == EXPECTED_VALIDATE_COLUMNS
    assert len(df) == 1
    # single_rate should be parseable — the notebook doesn't end in _sbs.py so
    # no missing-metric error, but the print is still captured
    assert df.iloc[0]["exit_code"] == 0


# ---------------------------------------------------------------------------
# Task 4 — sweep_headless + pick_sweep_winner
# ---------------------------------------------------------------------------


@needs_screen_env
def test_sweep_headless_returns_one_row_per_value(bw, tmp_path):
    """sweep_headless aggregates tiles → one row per param value."""
    nb = _make_parameterized_nb(tmp_path)
    # 3 values, 2 tiles each → 3 rows in output (aggregated by value)
    df = asyncio.run(
        bw.sweep_headless(
            nb_path=nb,
            param_name="THRESHOLD",
            values=[50, 60, 80],
            tiles=[(1, "A1", 1), (1, "A1", 2)],
            work_dir=tmp_path / "sweep_work",
            conda_env=SCREEN_ENV,
            omp_threads=1,
            max_parallel=4,
            timeout_s=60.0,
        )
    )
    assert set(df.columns) >= {"value", "mean_single_rate", "std_single_rate", "n_tiles", "qc_pass_frac"}
    assert len(df) == 3
    # n_tiles should be 2 per value (2 tiles × 1 plate/well)
    assert (df["n_tiles"] == 2).all()


@needs_screen_env
def test_sweep_headless_mean_rates_match_threshold(bw, tmp_path):
    """Mean single rates reflect the parameterized notebook's formula (THRESHOLD/100)."""
    nb = _make_parameterized_nb(tmp_path)
    df = asyncio.run(
        bw.sweep_headless(
            nb_path=nb,
            param_name="THRESHOLD",
            values=[50, 80],
            tiles=[(1, "A1", 1), (1, "A1", 2)],
            work_dir=tmp_path / "sweep_mean_work",
            conda_env=SCREEN_ENV,
            omp_threads=1,
            max_parallel=4,
            timeout_s=60.0,
        )
    )
    # THRESHOLD=50 → 0.50; THRESHOLD=80 → 0.80
    row_50 = df[df["value"] == 50].iloc[0]
    row_80 = df[df["value"] == 80].iloc[0]
    assert row_50["mean_single_rate"] == pytest.approx(0.50, abs=0.01)
    assert row_80["mean_single_rate"] == pytest.approx(0.80, abs=0.01)
    # Std should be near zero (both tiles same notebook)
    assert row_50["std_single_rate"] == pytest.approx(0.0, abs=0.01)


@needs_screen_env
def test_pick_sweep_winner_maximizes_metric(bw, tmp_path):
    """pick_sweep_winner returns the value with the highest mean_single_rate."""
    nb = _make_parameterized_nb(tmp_path)
    df = asyncio.run(
        bw.sweep_headless(
            nb_path=nb,
            param_name="THRESHOLD",
            values=[50, 60, 80],
            tiles=[(1, "A1", 1)],
            work_dir=tmp_path / "pick_work",
            conda_env=SCREEN_ENV,
            omp_threads=1,
            max_parallel=4,
            timeout_s=60.0,
        )
    )
    winner = bw.pick_sweep_winner(df, metric="mean_single_rate")
    assert winner == 80


def test_pick_sweep_winner_tiebreak_lowest(bw):
    """pick_sweep_winner breaks ties by returning the lowest value."""
    try:
        import pandas as pd
    except ImportError:
        pytest.skip("pandas not available")

    df = pd.DataFrame(
        [
            {"value": 100, "mean_single_rate": 0.7, "std_single_rate": 0.0, "n_tiles": 2, "qc_pass_frac": 1.0},
            {"value": 50, "mean_single_rate": 0.7, "std_single_rate": 0.0, "n_tiles": 2, "qc_pass_frac": 1.0},
            {"value": 200, "mean_single_rate": 0.6, "std_single_rate": 0.0, "n_tiles": 2, "qc_pass_frac": 1.0},
        ]
    )
    winner = bw.pick_sweep_winner(df, metric="mean_single_rate")
    assert winner == 50


def test_pick_sweep_winner_single_row(bw):
    """pick_sweep_winner works on a single-row DataFrame."""
    try:
        import pandas as pd
    except ImportError:
        pytest.skip("pandas not available")

    df = pd.DataFrame(
        [{"value": 42, "mean_single_rate": 0.9, "std_single_rate": 0.0, "n_tiles": 1, "qc_pass_frac": 1.0}]
    )
    winner = bw.pick_sweep_winner(df, metric="mean_single_rate")
    assert winner == 42


# ---------------------------------------------------------------------------
# Coarse-to-fine: refine_points + adaptive_sweep_headless
# ---------------------------------------------------------------------------


def test_refine_points_peak_width(bw):
    """±2 step 1 around a mid winner, excluding the coarse band."""
    pts = bw.refine_points(11, 2, 1, 3, 20, exclude=[3, 7, 11, 15, 19])
    assert pts == [9, 10, 12, 13]


def test_refine_points_threshold_step50(bw):
    """±100 step 50 around 700, excluding coarse points; winner dropped."""
    coarse = [100, 300, 500, 700, 900, 1100, 1300, 1500]
    pts = bw.refine_points(700, 100, 50, 100, 1500, exclude=coarse)
    assert pts == [600, 650, 750, 800]


def test_refine_points_clamps_to_max(bw):
    """Winner at the ceiling: refine never exceeds hi and skips the winner."""
    pts = bw.refine_points(20, 2, 1, 3, 20, exclude=[])
    assert pts == [18, 19]


def test_refine_points_clamps_to_min(bw):
    pts = bw.refine_points(3, 2, 1, 3, 20, exclude=[])
    assert pts == [4, 5]


def test_refine_points_zero_step_is_empty(bw):
    assert bw.refine_points(11, 2, 0, 3, 20) == []


def _fake_sweep_factory(peak_value):
    """Return an async stub for sweep_headless whose mean_single_rate peaks at
    `peak_value` (triangular), so the winner is deterministic per value set."""
    import pandas as pd

    async def _fake(nb_path, param_name, values, tiles, **kwargs):
        rows = [
            {
                "value": v,
                "mean_single_rate": 1.0 - abs(v - peak_value) / 1000.0,
                "std_single_rate": 0.0,
                "n_tiles": len(list(tiles)) if not isinstance(tiles, list) else len(tiles),
                "qc_pass_frac": 1.0,
            }
            for v in values
        ]
        return pd.DataFrame(rows)

    return _fake


def test_adaptive_sweep_runs_coarse_then_refine(bw, monkeypatch):
    """Coarse winner triggers a refine pass; combined df contains both, and a
    refine value that beats the coarse winner wins overall."""
    try:
        import pandas as pd  # noqa: F401
    except ImportError:
        pytest.skip("pandas not available")

    calls = []

    async def _spy(nb_path, param_name, values, tiles, **kwargs):
        calls.append(list(values))
        return await _fake_sweep_factory(12)(nb_path, param_name, values, tiles, **kwargs)

    # adaptive_sweep_headless calls sweep_headless in its own (brieflow_sweep)
    # namespace, so patch it there — patching the bw facade would not intercept.
    monkeypatch.setattr(sys.modules["brieflow_sweep"], "sweep_headless", _spy)
    spec = {
        "coarse": [3, 7, 11, 15, 19],
        "refine_radius": 2,
        "refine_step": 1,
        "min": 3,
        "max": 20,
    }
    df = asyncio.run(
        bw.adaptive_sweep_headless(
            "nb.py", "PEAK_WIDTH", spec, [(1, "A1", 1)],
            work_dir="wd", conda_env="env",
        )
    )
    # Two passes: coarse, then refine around coarse winner 11 -> [9,10,12,13]
    assert len(calls) == 2
    assert calls[0] == [3, 7, 11, 15, 19]
    assert calls[1] == [9, 10, 12, 13]
    # Combined df includes the refine value 12, which is the global optimum.
    assert bw.pick_sweep_winner(df) == 12


def test_adaptive_sweep_no_refine_when_coarse_empty(bw, monkeypatch):
    """Empty coarse result short-circuits — no refine pass."""
    try:
        import pandas as pd
    except ImportError:
        pytest.skip("pandas not available")

    calls = []

    async def _empty(nb_path, param_name, values, tiles, **kwargs):
        calls.append(list(values))
        return pd.DataFrame(
            columns=["value", "mean_single_rate", "std_single_rate", "n_tiles", "qc_pass_frac"]
        )

    # patch in brieflow_sweep's namespace (where adaptive_sweep_headless looks
    # it up), not on the bw facade.
    monkeypatch.setattr(sys.modules["brieflow_sweep"], "sweep_headless", _empty)
    spec = {"coarse": [3, 7], "refine_radius": 2, "refine_step": 1, "min": 3, "max": 20}
    df = asyncio.run(
        bw.adaptive_sweep_headless(
            "nb.py", "PEAK_WIDTH", spec, [(1, "A1", 1)],
            work_dir="wd", conda_env="env",
        )
    )
    assert df.empty
    assert len(calls) == 1  # only the coarse pass


# ---------------------------------------------------------------------------
# Cluster resolution selection: parse_resolution_table + select_final_resolutions
# ---------------------------------------------------------------------------

_RES_STDOUT = """\
Creating clusters for resolution: 0.1
  resolution 0.1: 3 clusters, median genes/cluster 300.0
Creating clusters for resolution: 9
  resolution 9: 42 clusters, median genes/cluster 22.0
Creating clusters for resolution: 11
  resolution 11: 60 clusters, median genes/cluster 18.0
Creating clusters for resolution: 13
  resolution 13: 80 clusters, median genes/cluster 16.0
Creating clusters for resolution: 100
  resolution 100: 900 clusters, median genes/cluster 1.0
"""


def test_parse_resolution_table(bw):
    rows = bw.parse_resolution_table(_RES_STDOUT)
    assert len(rows) == 5
    assert rows[0] == {"resolution": 0.1, "n_clusters": 3, "median_genes_per_cluster": 300.0}
    assert rows[1]["resolution"] == 9  # integer-valued -> int
    assert rows[3]["median_genes_per_cluster"] == 16.0


def test_select_final_resolutions_near_target(bw):
    rows = bw.parse_resolution_table(_RES_STDOUT)
    # target 20 +/- 5 -> [15, 25] -> core resolutions 9 (22), 11 (18), 13 (16);
    # fine-sweep expands the core span by pad=1 (step 1): [8..14].
    assert bw.select_final_resolutions(rows, target=20, tolerance=5) == [
        8, 9, 10, 11, 12, 13, 14
    ]


def test_select_final_resolutions_fallback_closest(bw):
    # No resolution lands in-band -> use the single closest to target, then
    # fine-sweep around it with pad=2 (step 1).
    rows = [
        {"resolution": 1, "n_clusters": 5, "median_genes_per_cluster": 95.0},
        {"resolution": 100, "n_clusters": 900, "median_genes_per_cluster": 1.0},
    ]
    assert bw.select_final_resolutions(rows, target=20, tolerance=5) == [
        98, 99, 100, 101, 102
    ]


def test_select_final_resolutions_empty(bw):
    assert bw.select_final_resolutions([], target=20, tolerance=5) == []


# ---------------------------------------------------------------------------
# select_initial_sbs_tiles: well-spread alignment anchors (farthest-point)
# ---------------------------------------------------------------------------


def test_farthest_point_indices_picks_corners(bw):
    # 3x3 grid; asking for 4 -> the 4 corners (max spatial spread)
    pos = [(x, y) for y in range(3) for x in range(3)]
    idx = bw._farthest_point_indices(pos, 4)
    assert sorted(pos[i] for i in idx) == [(0, 0), (0, 2), (2, 0), (2, 2)]


def test_farthest_point_indices_n_ge_m_returns_all(bw):
    pos = [(0, 0), (1, 1)]
    assert sorted(bw._farthest_point_indices(pos, 5)) == [0, 1]


def test_select_initial_sbs_tiles_spreads(bw, tmp_path):
    pd = pytest.importorskip("pandas")
    base = tmp_path / "brieflow_output" / "preprocess" / "metadata" / "sbs" / "1" / "A" / "1"
    base.mkdir(parents=True)
    # 3x3 grid, tiles 0..8 (y outer, x inner): corners are tiles 0,2,6,8
    rows = [{"tile": t, "x_pos": x, "y_pos": y}
            for t, (x, y) in enumerate((x, y) for y in range(3) for x in range(3))]
    pd.DataFrame(rows).to_parquet(base / "combined_metadata.parquet")
    res = bw.select_initial_sbs_tiles(tmp_path, n_tiles=4)
    assert sorted(res["tiles"]) == [0, 2, 6, 8]
    assert res["well"] == "A1"


def test_select_initial_sbs_tiles_no_metadata_returns_empty(bw, tmp_path):
    res = bw.select_initial_sbs_tiles(tmp_path, n_tiles=4)
    assert res["tiles"] == []


def _anchor_fixture(tmp_path, pd, cells_by_well):
    """3x3 tile grid plus per-well SBS cell counts, for the min_cells gate."""
    meta = tmp_path / "brieflow_output" / "preprocess" / "metadata" / "sbs" / "1" / "A" / "1"
    meta.mkdir(parents=True)
    rows = [{"tile": t, "x_pos": x, "y_pos": y}
            for t, (x, y) in enumerate((x, y) for y in range(3) for x in range(3))]
    pd.DataFrame(rows).to_parquet(meta / "combined_metadata.parquet")
    for well, per_tile in cells_by_well.items():
        d = tmp_path / "brieflow_output" / "sbs" / "parquets" / "1" / well[0] / well[1:]
        d.mkdir(parents=True)
        recs = [{"tile": t} for t, n in per_tile.items() for _ in range(n)]
        pd.DataFrame(recs or [{"tile": -1}]).to_parquet(d / "sbs_info.parquet")


def test_anchor_gate_tolerates_a_few_dead_wells(bw, tmp_path):
    """One near-empty well must not veto every tile (a sparse screen: healthy median,
    five dead wells, zero anchors selected, merge notebook died on None)."""
    pd = pytest.importorskip("pandas")
    good = {t: 100 for t in range(9)}
    wells = {f"A{i}": dict(good) for i in range(1, 11)}
    wells["A10"] = {t: 0 for t in range(9)}          # one dead well out of ten
    _anchor_fixture(tmp_path, pd, wells)
    res = bw.select_initial_sbs_tiles(tmp_path, n_tiles=4)  # default frac
    assert sorted(res["tiles"]) == [0, 2, 6, 8], res["rationale"]


def test_anchor_gate_still_drops_a_broadly_sparse_tile(bw, tmp_path):
    """A tile sparse across most wells is still excluded — the gate is relaxed, not off."""
    pd = pytest.importorskip("pandas")
    wells = {}
    for i in range(1, 11):
        per = {t: 100 for t in range(9)}
        per[4] = 1                                    # tile 4 sparse in every well
        wells[f"A{i}"] = per
    _anchor_fixture(tmp_path, pd, wells)
    res = bw.select_initial_sbs_tiles(tmp_path, n_tiles=9)
    assert 4 not in res["tiles"], res["rationale"]
    assert len(res["tiles"]) == 8
