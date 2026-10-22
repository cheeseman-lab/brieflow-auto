"""Tests for branch-and-converge SBS (per-plate config divergence).

Covers the runner helpers (load_plate_overrides, _read_sbs_plate_cycles,
generate_plate_configs, run_sbs_branched) plus the --only-plate pass-through
through runloop.build_run_cmd and the flow.sh arm. The full pipeline run isn't
exercised (needs slurm + data); the wiring and config-generation logic are.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runner.py"
RUNLOOP_PY = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runloop.py"
RUN_PY = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_run.py"


@pytest.fixture(scope="module")
def ap():
    spec = importlib.util.spec_from_file_location("brieflow_runner", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runner"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def runloop():
    spec = importlib.util.spec_from_file_location("brieflow_runloop", RUNLOOP_PY)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runloop"] = m
    spec.loader.exec_module(m)
    return m


def _screen_yaml(tmp_path: Path, plate_overrides: dict | None) -> Path:
    iss = {"cycles": 12}
    if plate_overrides is not None:
        iss["plate_overrides"] = plate_overrides
    (tmp_path / "screen.yaml").write_text(yaml.safe_dump({"iss": iss}))
    return tmp_path


def _write_base_config(tmp_path: Path) -> str:
    cfg = {
        "all": {"root_fp": "brieflow_output/"},
        "preprocess": {"sbs_samples_fp": "config/sbs_samples.tsv"},
        "sbs": {"skip_cycles_indices": None, "manual_background_cycle_index": None,
                "threshold_reads": 200},
    }
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "config.yml").write_text(yaml.safe_dump(cfg))
    return "config/config.yml"


def _write_sbs_samples(tmp_path: Path, plate_cycles: dict[int, list[int]]) -> None:
    (tmp_path / "config").mkdir(exist_ok=True)
    rows = ["plate\tcycle\twell\ttile"]
    for plate, cycles in plate_cycles.items():
        for c in cycles:
            rows.append(f"{plate}\t{c}\tA1\t1")
    (tmp_path / "config" / "sbs_samples.tsv").write_text("\n".join(rows) + "\n")


# ---------------------------------------------------------------------------
# load_plate_overrides
# ---------------------------------------------------------------------------


def test_load_plate_overrides_absent_is_empty(ap, tmp_path):
    _screen_yaml(tmp_path, None)
    assert ap.load_plate_overrides(tmp_path) == {}


def test_load_plate_overrides_parses_int_keys(ap, tmp_path):
    _screen_yaml(tmp_path, {1: {"skip_cycles": [1]}, 2: {}})
    ov = ap.load_plate_overrides(tmp_path)
    assert ov == {1: {"skip_cycles": [1]}, 2: {}}
    assert all(isinstance(k, int) for k in ov)


def test_load_plate_overrides_no_screen_yaml(ap, tmp_path):
    assert ap.load_plate_overrides(tmp_path) == {}


# ---------------------------------------------------------------------------
# _read_sbs_plate_cycles
# ---------------------------------------------------------------------------


def test_read_sbs_plate_cycles(ap, tmp_path):
    _write_sbs_samples(tmp_path, {1: [1, 2, 3, 12], 2: [1, 2, 3]})
    got = ap._read_sbs_plate_cycles(tmp_path / "config" / "sbs_samples.tsv")
    assert got == {1: [1, 2, 3, 12], 2: [1, 2, 3]}


def test_read_sbs_plate_cycles_missing_file(ap, tmp_path):
    assert ap._read_sbs_plate_cycles(tmp_path / "nope.tsv") == {}


# ---------------------------------------------------------------------------
# generate_plate_configs
# ---------------------------------------------------------------------------


def test_generate_plate_configs_skip_translates_to_index(ap, tmp_path):
    """jannu shape: plate_1 c1..c12 skips cycle 1 (index 0); plate_2 c1..c11 none."""
    base = _write_base_config(tmp_path)
    plate_cycles = {1: list(range(1, 13)), 2: list(range(1, 12))}
    overrides = {1: {"skip_cycles": [1]}}
    out = ap.generate_plate_configs(tmp_path, base, overrides, plate_cycles)

    assert set(out) == {1, 2}
    p1 = yaml.safe_load((tmp_path / out[1]).read_text())
    p2 = yaml.safe_load((tmp_path / out[2]).read_text())
    assert p1["sbs"]["skip_cycles_indices"] == [0]      # cycle 1 -> index 0
    assert p2["sbs"]["skip_cycles_indices"] is None     # no override -> none
    # base scalars preserved in the copy
    assert p1["sbs"]["threshold_reads"] == 200


def test_generate_plate_configs_manual_background(ap, tmp_path):
    base = _write_base_config(tmp_path)
    plate_cycles = {1: list(range(1, 13))}
    overrides = {1: {"skip_cycles": [1], "manual_background_cycle": 2}}
    out = ap.generate_plate_configs(tmp_path, base, overrides, plate_cycles)
    p1 = yaml.safe_load((tmp_path / out[1]).read_text())
    assert p1["sbs"]["manual_background_cycle_index"] == 1   # cycle 2 -> index 1


def test_generate_plate_configs_missing_cycle_raises(ap, tmp_path):
    base = _write_base_config(tmp_path)
    plate_cycles = {2: list(range(1, 12))}     # plate_2 has no cycle 12
    overrides = {2: {"skip_cycles": [12]}}
    with pytest.raises(ValueError, match="not present"):
        ap.generate_plate_configs(tmp_path, base, overrides, plate_cycles)


# ---------------------------------------------------------------------------
# run_sbs_branched — per-plate loop wiring (run_one_phase monkeypatched)
# ---------------------------------------------------------------------------


def test_run_sbs_branched_runs_each_plate(ap, tmp_path, monkeypatch):
    base = _write_base_config(tmp_path)
    _write_sbs_samples(tmp_path, {1: list(range(1, 13)), 2: list(range(1, 12))})
    overrides = {1: {"skip_cycles": [1]}}

    calls = []

    def fake_run_one_phase(args, modules, ptag, notes):
        calls.append({"config": args.config, "only_plate": args.only_plate,
                      "modules": list(modules)})
        return 0

    monkeypatch.setattr(ap, "run_one_phase", fake_run_one_phase)
    monkeypatch.setattr(ap, "phase_tag", lambda tag, i, mods: f"{tag}_{i}")
    monkeypatch.setattr(ap, "notify", lambda *a, **k: None)

    args = SimpleNamespace(config=base, only_plate=None)
    rc, failed = ap.run_sbs_branched(args, tmp_path, overrides, "tag", "notes")

    assert rc == 0 and failed is None
    assert [c["only_plate"] for c in calls] == [1, 2]
    assert calls[0]["config"] == "config/config_p1.yml"
    assert calls[1]["config"] == "config/config_p2.yml"
    assert all(c["modules"] == ["sbs"] for c in calls)
    # args restored after the loop
    assert args.config == base and args.only_plate is None


def test_run_sbs_branched_stops_on_failure(ap, tmp_path, monkeypatch):
    base = _write_base_config(tmp_path)
    _write_sbs_samples(tmp_path, {1: list(range(1, 13)), 2: list(range(1, 12))})
    overrides = {1: {"skip_cycles": [1]}}

    def fake_run_one_phase(args, modules, ptag, notes):
        return 2 if args.only_plate == 1 else 0  # plate 1 fails

    monkeypatch.setattr(ap, "run_one_phase", fake_run_one_phase)
    monkeypatch.setattr(ap, "phase_tag", lambda tag, i, mods: f"{tag}_{i}")
    monkeypatch.setattr(ap, "notify", lambda *a, **k: None)

    args = SimpleNamespace(config=base, only_plate=None)
    rc, failed = ap.run_sbs_branched(args, tmp_path, overrides, "tag", "notes")
    assert rc == 2 and failed == 1


# ---------------------------------------------------------------------------
# --only-plate pass-through (runloop → run.py → flow.sh)
# ---------------------------------------------------------------------------


def _args(**overrides) -> SimpleNamespace:
    base = dict(
        analysis_dir=Path("/tmp/fake"), backend="slurm", config="config/config.yml",
        cpus_per_task=1, array_limit=10, jobs=400, latency_wait=60, mem_recs=None,
        use_arrays=False, forcerun=[], cores=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_run_cmd_no_only_plate(runloop):
    cmd = runloop.build_run_cmd(_args(), ["sbs"], "tag", "", 1)
    assert "--only-plate" not in cmd


def test_build_run_cmd_with_only_plate(runloop):
    cmd = runloop.build_run_cmd(_args(only_plate=2), ["sbs"], "tag", "", 1)
    assert "--only-plate" in cmd
    assert cmd[cmd.index("--only-plate") + 1] == "2"


def test_brieflow_run_help_lists_only_plate():
    r = subprocess.run([sys.executable, str(RUN_PY), "--help"], capture_output=True, text=True)
    assert r.returncode == 0
    assert "--only-plate" in r.stdout
