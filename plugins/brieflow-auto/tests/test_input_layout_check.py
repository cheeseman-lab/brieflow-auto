"""The runner refuses raw inputs that are not in the layout brieflow reads (exit 9)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import brieflow_runner as rn  # noqa: E402


def _touch(d: Path, n: int = 1) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"img_{i}.nd2").write_text("x")


def _screen(tmp_path: Path, rounds: int = 1) -> Path:
    data = tmp_path / "data"
    for c in (1, 2):
        _touch(data / "input_sbs" / "plate_1" / f"c{c}")
    for r in range(1, rounds + 1):
        _touch(data / "input_ph" / "plate_1" / f"round_{r}")
    analysis = tmp_path / "analysis"
    (analysis / ".brieflow").mkdir(parents=True)
    (analysis / "screen.yaml").write_text(
        f"iss: {{cycles: 2}}\ndata: {{input_sbs_dir: {data / 'input_sbs'}, "
        f"input_phenotype_dir: {data / 'input_ph'}}}\n"
    )
    return analysis


def test_canonical_inputs_pass(tmp_path):
    assert rn.input_layout_check(_screen(tmp_path), "t") == 0


def test_cycles_in_one_folder_stop_the_run(tmp_path):
    analysis = _screen(tmp_path)
    sbs = tmp_path / "data" / "input_sbs" / "plate_1"
    for c in ("c1", "c2"):
        for f in (sbs / c).iterdir():
            f.rename(sbs / f"{c}_{f.name}")
        (sbs / c).rmdir()
    assert rn.input_layout_check(analysis, "t") == rn._RC_DATA_LAYOUT
    progress = json.loads((analysis / ".brieflow" / "progress.json").read_text())
    assert progress["paused"]["reason"] == "input data layout"


def test_uneven_rounds_need_the_operator(tmp_path):
    analysis = _screen(tmp_path, rounds=2)
    _touch(tmp_path / "data" / "input_ph" / "plate_1" / "round_2", n=3)
    assert rn.input_layout_check(analysis, "t") == rn._RC_DATA_LAYOUT
    assert rn.input_layout_check(analysis, "t", accept_warnings=True) == 0
    journal = (analysis / ".brieflow" / "decisions.jsonl").read_text()
    assert "layout-accept" in journal
