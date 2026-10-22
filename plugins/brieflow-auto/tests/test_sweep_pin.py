"""screen.yaml `sweep.tiles` / `sweep.wells` pin the sweep scope: a line whose
signal lives in a few wells is tuned on those wells, not on a plate-wide sample."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

_spec = importlib.util.spec_from_file_location("brieflow_resolvers", SCRIPTS / "brieflow_resolvers.py")
_br = importlib.util.module_from_spec(_spec)
sys.modules["brieflow_resolvers"] = _br
_spec.loader.exec_module(_br)


def _tiles():
    return [(1, f"r0{r}c{c:02d}", 1) for r in (2, 3, 4) for c in range(2, 6)]


def test_no_pin_is_a_noop(tmp_path):
    pin = _br._pinned_sweep(tmp_path)
    assert pin == {"tiles": [], "wells": set()}
    assert _br._apply_sweep_pin(_tiles(), pin) == _tiles()


def test_wells_pin_restricts_sample(tmp_path):
    (tmp_path / "screen.yaml").write_text("sweep:\n  wells: [r04c02, r04c05]\n")
    pin = _br._pinned_sweep(tmp_path)
    out = _br._apply_sweep_pin(_tiles(), pin)
    assert [t[1] for t in out] == ["r04c02", "r04c05"]


def test_wells_pin_with_no_match_falls_back(tmp_path):
    (tmp_path / "screen.yaml").write_text("sweep:\n  wells: [r09c09]\n")
    out = _br._apply_sweep_pin(_tiles(), _br._pinned_sweep(tmp_path))
    assert out == _tiles()


def test_explicit_tiles_win(tmp_path):
    (tmp_path / "screen.yaml").write_text(
        "sweep:\n  wells: [r04c02]\n  tiles: [[1, r04c06, 1], [1, r04c11, 5]]\n")
    out = _br._apply_sweep_pin(_tiles(), _br._pinned_sweep(tmp_path))
    assert out == [(1, "r04c06", 1), (1, "r04c11", 5)]


def test_malformed_yaml_is_ignored(tmp_path):
    (tmp_path / "screen.yaml").write_text("sweep: [not, a, mapping\n")
    assert _br._pinned_sweep(tmp_path) == {"tiles": [], "wells": set()}
