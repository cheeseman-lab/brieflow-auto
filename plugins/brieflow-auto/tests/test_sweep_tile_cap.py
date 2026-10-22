"""_bounded_sweep_tiles: cap the reactive sweep to a bounded, well-spread sample
so sweep cost stays plate-size-independent (v0.5.4). Fixes the one-tile-per-well
fan-out that made large-plate sweeps O(n_wells x values)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location(
    "brieflow_resolvers", SCRIPTS / "brieflow_resolvers.py"
)
_br = importlib.util.module_from_spec(_spec)
sys.modules["brieflow_resolvers"] = _br
_spec.loader.exec_module(_br)
_bound = _br._bounded_sweep_tiles


def _tiles(n):
    # (plate, well, tile) tuples in the (plate,row,col)-sorted order the tile
    # selectors emit, so an even subsample is well-spread across the plate.
    return [(1, f"r{2 + i // 10:02d}c{2 + i % 10:02d}", 1) for i in range(n)]


def test_cap_large_plate_to_max():
    tiles = _tiles(60)
    out = _bound(tiles, 10)
    assert len(out) == 10
    assert all(t in tiles for t in out)


def test_spread_is_monotonic_and_distinct():
    tiles = _tiles(60)
    out = _bound(tiles, 10)
    idxs = [tiles.index(t) for t in out]
    assert idxs[0] == 0                    # starts at the front
    assert len(set(idxs)) == 10            # no duplicates
    assert idxs == sorted(idxs)            # order-preserved / well-spread


def test_no_cap_when_within_limit():
    tiles = _tiles(6)
    out = _bound(tiles, 10)
    assert out == tiles
    assert out is not tiles                # returns a copy, not the input list


def test_empty_stays_empty():
    assert _bound([], 10) == []


def test_falsy_max_is_unbounded():
    tiles = _tiles(60)
    assert _bound(tiles, 0) == tiles
    assert _bound(tiles, None) == tiles
