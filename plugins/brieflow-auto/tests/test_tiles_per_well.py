"""Per-well fan-out: select_tiles_one_per_well picks exactly one central interior
tile for every well, so no well enters ph/sbs without a seg/align diagnostic.
"""
from __future__ import annotations

import pandas as pd


def _grid(n: int = 3, spacing: int = 100):
    """n×n grid of tiles; the geometric-center tile is id (n*n)//2 for odd n."""
    out = []
    tid = 0
    for r in range(n):
        for c in range(n):
            out.append((tid, c * spacing, r * spacing))
            tid += 1
    return out


def _write_well(base, plate, well, tiles):
    d = base / plate / well[0] / well[1:]
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(tiles, columns=["tile", "x_pos", "y_pos"]).to_parquet(
        d / "combined_metadata.parquet"
    )


def test_one_central_tile_per_well(load_script, tmp_path):
    tiles = load_script("brieflow_tiles")
    base = tmp_path / "brieflow_output" / "preprocess" / "metadata" / "sbs"
    # 3 wells on plate 1, 1 well on plate 2 → every well must be covered.
    for plate, well in [("1", "A1"), ("1", "A2"), ("1", "B1"), ("2", "A1")]:
        _write_well(base, plate, well, _grid())

    out = tiles.select_tiles_one_per_well(tmp_path, "sbs")

    assert out["n_wells"] == 4
    assert {(p, w) for p, w, _ in out["tiles"]} == {
        (1, "A1"), (1, "A2"), (1, "B1"), (2, "A1")
    }
    # the central tile of a 3×3 grid is id 4 (r=1,c=1 at the centroid)
    assert all(t == 4 for _, _, t in out["tiles"])
    # numeric plate names cast to int (parquet column consistency)
    assert all(isinstance(p, int) for p, _, _ in out["tiles"])
    assert out["wells_without_metadata"] == []


def test_missing_metadata_returns_empty_not_tile0(load_script, tmp_path):
    tiles = load_script("brieflow_tiles")
    out = tiles.select_tiles_one_per_well(tmp_path, "phenotype")  # no metadata tree
    assert out["tiles"] == []
    assert out["n_wells"] == 0
    assert "not found" in out["rationale"]


def test_unreadable_well_is_flagged_not_silently_dropped(load_script, tmp_path):
    tiles = load_script("brieflow_tiles")
    base = tmp_path / "brieflow_output" / "preprocess" / "metadata" / "sbs"
    _write_well(base, "1", "A1", _grid())
    bad = base / "1" / "A" / "2"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "combined_metadata.parquet").write_bytes(b"not a parquet")

    out = tiles.select_tiles_one_per_well(tmp_path, "sbs")

    assert out["n_wells"] == 1  # A1 resolved
    assert (1, "A1") in {(p, w) for p, w, _ in out["tiles"]}
    assert (1, "A2") in out["wells_without_metadata"]  # surfaced, not hidden


def test_test_tile_from_a_well_with_three_tiles(load_script, tmp_path):
    """A well with fewer interior tiles than requested still yields its tiles, no IndexError."""
    tiles = load_script("brieflow_tiles")
    base = tmp_path / "brieflow_output" / "preprocess" / "metadata" / "sbs"
    _write_well(base, "1", "A1", [(0, 0, 0), (1, 100, 0), (2, 50, 80)])
    out = tiles.select_test_tile_from_metadata(tmp_path, "sbs", n_tiles=3, plate=1, well="A1")
    assert out["tiles"] and set(out["tiles"]) <= {0, 1, 2}
