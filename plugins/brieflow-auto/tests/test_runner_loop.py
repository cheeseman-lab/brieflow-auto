"""Conductor loop tests (T8) — all external dependencies monkeypatched.

Tests:
  - happy_path: all 6 phases advance, returns 0, mark_phase_done called per phase.
  - trigger_1: QC gate fails on sbs → returns nonzero, action_needed notify, set_paused.
  - trigger_2: run_one_phase returns nonzero on sbs → pause+notify before QC.
  - trigger_4: check_disk_available → {ok: False} on first phase → pause+notify.
  - resume: pre-mark preprocess done → loop skips it.
  - notebook_run_failure: run_phase_notebook fails → RC5, pause+notify, run_one_phase not called.
  - missing_conda_env: _conda_env returns None → pause+notify, no notebook run.
  - notebook_before_snakemake: run_phase_notebook is called before run_one_phase per phase.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runner.py"
PHASES = ["preprocess", "sbs", "phenotype", "merge", "classify", "aggregate", "cluster", "analyze"]
# classify and analyze are notebook-only: they are configured and marked done
# like any other phase, but have no snakemake module, so they never reach
# run_one_phase.
RUN_PHASES = [p for p in PHASES if p not in ("classify", "analyze")]


@pytest.fixture(scope="module")
def ap():
    spec = importlib.util.spec_from_file_location("brieflow_runner", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runner"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _stub_cluster_final(ap, monkeypatch):
    """Cluster FINAL_LEIDEN_RESOLUTIONS selection is covered in test_runner.py.
    Here stub it so the loop reaches/completes the cluster phase deterministically
    (it otherwise runs an extra notebook pass + needs a parseable table)."""
    monkeypatch.setattr(ap, "resolve_cluster_final", lambda *a, **k: [11])


@pytest.fixture(autouse=True)
def _stub_preflight(ap, monkeypatch):
    """Preflight runs the real brieflow_preflight.py subprocess (needs a brieflow
    submodule + config). Default it to clean (0) so loop tests reach the run step;
    the preflight-fail trigger test overrides this."""
    monkeypatch.setattr(ap, "run_preflight", lambda *a, **k: 0)


@pytest.fixture(autouse=True)
def _approve_launch(ap, monkeypatch):
    """The F1 launch gate is covered in test_runner_range.py. These args namespaces
    carry no --approved, so approve every run here and let the loop proceed."""
    monkeypatch.setattr(ap, "write_launch_review", lambda *a, **k: {"review_sha256": "x"})
    monkeypatch.setattr(ap, "launch_approved", lambda *a, **k: True)


def _make_args(analysis_dir: Path) -> argparse.Namespace:
    """Build a minimal args namespace matching run_one_phase's expectations."""
    return argparse.Namespace(
        analysis_dir=analysis_dir,
        tag="run_test",
        config="config/config.yml",
        backend="slurm",
        jobs=400,
        array_limit=10,
        use_arrays=False,
        latency_wait=60,
        cpus_per_task=1,
        mem_recs=None,
        max_attempts=3,
        no_aggregate_efficiency=True,
        require_rerun=None,
        forcerun=[],
        oom_cascade_threshold=3,
        oom_poll_seconds=30,
        disk_warn_pct=80,
        disk_abort_pct=90,
    )


def _make_qc_summary(phase: str, pass_=True):
    """Return a mock QCSummary-like object."""
    m = MagicMock()
    m.phase = phase
    m.metrics = []
    m.overall_status = "pass" if pass_ else "fail"
    return m


def _make_sweep_df():
    """Return a minimal sweep DataFrame."""
    return pd.DataFrame({
        "value": [100, 200, 400],
        "mean_single_rate": [0.5, 0.7, 0.6],
        "std_single_rate": [0.05, 0.03, 0.04],
        "n_tiles": [3, 3, 3],
        "qc_pass_frac": [1.0, 1.0, 1.0],
    })


_NB_OK = {"ok": True, "returncode": 0, "log": Path("/dev/null")}


def _run_with_patches(func, ap_module, patch_specs):
    """Apply a list of patch.object specs via ExitStack, run func, return result."""
    with ExitStack() as stack:
        for p in patch_specs:
            stack.enter_context(p)
        return func()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_zero(ap, tmp_path):
    args = _make_args(tmp_path)
    # Stub analysis dir with required dirs/files so conductor doesn't choke
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    notify_calls = []
    mark_done_calls = []
    sweep_df = _make_sweep_df()

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {"sbs": {"THRESHOLD_READS": [100, 200]}},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={"n_plates": 1}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 100,
            "phase_breakdown": {p: 10 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "sweep_headless", return_value=sweep_df),
        patch.object(ap, "pick_sweep_winner", return_value=200),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase", return_value=0),
        patch.object(ap, "build_phase_qc", return_value=_make_qc_summary("sbs", pass_=True)),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": [], "rationale": "ok"}),
        patch.object(ap, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))),
        patch.object(ap, "mark_phase_done", side_effect=lambda d, ph: mark_done_calls.append(ph)),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert rc == 0, f"Expected 0, got {rc}"
    assert set(mark_done_calls) == set(PHASES), f"Not all phases marked done: {mark_done_calls}"

    # The completion message is intentionally action_needed (M3) so the operator
    # is pinged that the run finished.  Assert no *failure* action_needed fires —
    # i.e. any action_needed call must reference "COMPLETE" not a gate failure.
    failure_action_needed = [
        (a, kw) for (a, kw) in notify_calls
        if (kw.get("priority") == "action_needed" or (len(a) >= 4 and a[3] == "action_needed"))
        and "COMPLETE" not in (a[2] if len(a) >= 3 else "")
    ]
    assert failure_action_needed == [], f"Unexpected failure action_needed calls: {failure_action_needed}"


def test_happy_path_run_one_phase_called_for_each_phase(ap, tmp_path):
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()
    run_calls = []

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=100),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase",
                     side_effect=lambda a, mods, tag, notes: run_calls.append(mods[0]) or 0),
        patch.object(ap, "build_phase_qc", side_effect=lambda *a, **kw: _make_qc_summary(a[1], True)),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify"),
        patch.object(ap, "mark_phase_done"),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]
    _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert run_calls == RUN_PHASES, f"run_one_phase calls: {run_calls}"


# ---------------------------------------------------------------------------
# Trigger 1: QC gate fails on sbs
# ---------------------------------------------------------------------------


def test_trigger1_qc_fail_on_sbs_returns_nonzero(ap, tmp_path):
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    notify_calls = []
    mark_done_calls = []

    def qc_verdict_stub(summary, **kw):
        if summary.phase == "sbs":
            return {"pass": False, "failures": ["single_map_rate"], "rationale": "sbs: fail"}
        return {"pass": True, "failures": []}

    def build_qc_stub(*a, **kw):
        phase = a[1]
        return _make_qc_summary(phase, pass_=(phase != "sbs"))

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=100),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase", return_value=0),
        patch.object(ap, "build_phase_qc", side_effect=build_qc_stub),
        patch.object(ap, "qc_gate_verdict", side_effect=qc_verdict_stub),
        patch.object(ap, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))),
        patch.object(ap, "mark_phase_done",
                     side_effect=lambda d, ph: mark_done_calls.append(ph)),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert rc != 0, "Expected nonzero return on QC gate failure"

    def _is_action_needed(call):
        a, kw = call
        return kw.get("priority") == "action_needed" or (len(a) >= 4 and a[3] == "action_needed")

    assert any(_is_action_needed(c) for c in notify_calls), (
        "Expected notify with priority=action_needed on trigger 1"
    )

    # Phases after sbs must NOT have been marked done
    assert "phenotype" not in mark_done_calls
    assert "merge" not in mark_done_calls


def test_trigger1_set_paused_called_on_qc_fail(ap, tmp_path):
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    set_paused_calls = []

    def qc_verdict_stub(summary, **kw):
        if summary.phase == "sbs":
            return {"pass": False, "failures": ["single_map_rate"],
                    "rationale": "sbs: single_map_rate=0.1"}
        return {"pass": True, "failures": []}

    def build_qc_stub(*a, **kw):
        phase = a[1]
        return _make_qc_summary(phase, pass_=(phase != "sbs"))

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=100),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase", return_value=0),
        patch.object(ap, "build_phase_qc", side_effect=build_qc_stub),
        patch.object(ap, "qc_gate_verdict", side_effect=qc_verdict_stub),
        patch.object(ap, "notify"),
        patch.object(ap, "mark_phase_done"),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused",
                     side_effect=lambda d, ph, reason: set_paused_calls.append((ph, reason))),
    ]
    _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert set_paused_calls, "set_paused must be called on trigger 1"
    phase, reason = set_paused_calls[0]
    assert phase == "sbs"
    # reason should mention the failing metric
    assert "single_map_rate" in reason or "sbs" in reason


# ---------------------------------------------------------------------------
# Trigger 2: run_one_phase returns nonzero on sbs
# ---------------------------------------------------------------------------


def test_trigger2_run_failure_returns_nonzero(ap, tmp_path):
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    notify_calls = []
    mark_done_calls = []

    def run_stub(a, mods, tag, notes):
        if mods[0] == "sbs":
            return 1
        return 0

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=100),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase", side_effect=run_stub),
        patch.object(ap, "build_phase_qc",
                     side_effect=lambda *a, **kw: _make_qc_summary(a[1], True)),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))),
        patch.object(ap, "mark_phase_done",
                     side_effect=lambda d, ph: mark_done_calls.append(ph)),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert rc != 0

    def _is_action_needed(call):
        a, kw = call
        return kw.get("priority") == "action_needed" or (len(a) >= 4 and a[3] == "action_needed")

    assert any(_is_action_needed(c) for c in notify_calls)
    assert "phenotype" not in mark_done_calls


# ---------------------------------------------------------------------------
# Trigger 6: preflight fails on sbs → pause+notify BEFORE submit (run_one_phase
# not called). Completes the cycle's notebook → preflight → run → QC ordering.
# ---------------------------------------------------------------------------


def test_trigger6_preflight_failure_blocks_submit(ap, tmp_path):
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    notify_calls = []
    run_calls = []
    mark_done_calls = []

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        # Preflight reports a HIGH-severity issue on the first phase.
        patch.object(ap, "run_preflight", return_value=1),
        patch.object(ap, "run_one_phase",
                     side_effect=lambda a, mods, tag, notes: run_calls.append(mods[0]) or 0),
        patch.object(ap, "build_phase_qc",
                     side_effect=lambda *a, **kw: _make_qc_summary(a[1], True)),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))),
        patch.object(ap, "mark_phase_done",
                     side_effect=lambda d, ph: mark_done_calls.append(ph)),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert rc == ap._RC_PREFLIGHT_FAIL, f"expected preflight-fail rc, got {rc}"
    # Preflight gates submit: run_one_phase must NOT have been called.
    assert run_calls == [], f"run_one_phase ran despite preflight failure: {run_calls}"
    assert mark_done_calls == [], f"no phase should complete: {mark_done_calls}"

    def _is_action_needed(call):
        a, kw = call
        return kw.get("priority") == "action_needed" or (len(a) >= 4 and a[3] == "action_needed")

    assert any(_is_action_needed(c) for c in notify_calls)


def test_trigger2_qc_not_called_after_run_failure(ap, tmp_path):
    """build_phase_qc must NOT be called after run_one_phase fails."""
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    qc_calls = []

    def run_stub(a, mods, tag, notes):
        if mods[0] == "sbs":
            return 1
        return 0

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=100),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase", side_effect=run_stub),
        patch.object(ap, "build_phase_qc",
                     side_effect=lambda *a, **kw: qc_calls.append(a[1]) or _make_qc_summary(a[1])),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify"),
        patch.object(ap, "mark_phase_done"),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]
    _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    # sbs should NOT appear in qc_calls since run failed before QC
    assert "sbs" not in qc_calls, f"build_phase_qc called for sbs after run failure: {qc_calls}"


# ---------------------------------------------------------------------------
# Trigger 4: check_disk_available → ok=False on first phase
# ---------------------------------------------------------------------------


def test_trigger4_disk_unavailable_returns_nonzero(ap, tmp_path):
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    notify_calls = []
    run_calls = []

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 100,
            "phase_breakdown": {p: 10 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available",
                     return_value={"ok": False, "available_gb": 5, "required_gb": 10}),
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=100),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase",
                     side_effect=lambda *a: run_calls.append(a) or 0),
        patch.object(ap, "build_phase_qc",
                     side_effect=lambda *a, **kw: _make_qc_summary(a[1])),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))),
        patch.object(ap, "mark_phase_done"),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert rc != 0

    def _is_action_needed(call):
        a, kw = call
        return kw.get("priority") == "action_needed" or (len(a) >= 4 and a[3] == "action_needed")

    assert any(_is_action_needed(c) for c in notify_calls), (
        "Expected action_needed notify on disk unavailability"
    )

    # run_one_phase must NOT have been called
    assert run_calls == [], f"run_one_phase should not run when disk unavailable: {run_calls}"


# ---------------------------------------------------------------------------
# Resume: pre-mark preprocess done → loop skips it
# ---------------------------------------------------------------------------


def test_resume_skips_done_phases(ap, tmp_path):
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    run_calls = []

    def phase_done_stub(analysis_dir, phase):
        return phase == "preprocess"

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=100),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase",
                     side_effect=lambda a, mods, tag, notes: run_calls.append(mods[0]) or 0),
        patch.object(ap, "build_phase_qc",
                     side_effect=lambda *a, **kw: _make_qc_summary(a[1])),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify"),
        patch.object(ap, "mark_phase_done"),
        patch.object(ap, "phase_done", side_effect=phase_done_stub),
        patch.object(ap, "set_paused"),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert rc == 0
    assert "preprocess" not in run_calls, "preprocess should have been skipped (already done)"
    assert "sbs" in run_calls
    assert set(run_calls) == set(RUN_PHASES) - {"preprocess"}


# ---------------------------------------------------------------------------
# Trigger 3: resolve_all_phases has gaps at headless launch
# ---------------------------------------------------------------------------


def test_trigger3_gaps_returns_nonzero(ap, tmp_path):
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    notify_calls = []

    patches = [
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "resolve_all_phases", return_value={
            "filled": [],
            "gaps": [{"param_name": "THRESHOLD_READS", "notebook": "sbs", "notes": ""}],
        }),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=100),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase", return_value=0),
        patch.object(ap, "build_phase_qc",
                     side_effect=lambda *a, **kw: _make_qc_summary(a[1])),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))),
        patch.object(ap, "mark_phase_done"),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert rc != 0, "Expected nonzero return on template gap (trigger 3)"

    def _is_action_needed(call):
        a, kw = call
        return kw.get("priority") == "action_needed" or (len(a) >= 4 and a[3] == "action_needed")

    assert any(_is_action_needed(c) for c in notify_calls)


# ---------------------------------------------------------------------------
# M2: sbs sweep resolves tiles and patches the winner
# ---------------------------------------------------------------------------


def test_sbs_sweep_resolves_tiles_and_patches(ap, tmp_path):
    """Happy-path run: sbs sweep resolves tiles and patches the winner."""
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    # Create the sbs notebook file so nb_path.exists() is True
    nb_path = tmp_path / "2_sbs.py"
    nb_path.write_text("# sbs notebook\n")

    tiles_called = []
    patch_calls = []
    sweep_df = _make_sweep_df()

    def tiles_stub(analysis_dir, phase):
        # per-well fan-out: one tile per well, scaled to the dataset (no n_tiles cap)
        tiles_called.append((analysis_dir, phase))
        return {"tiles": [(1, "A1", 5)]}

    def patch_stub(nb, param, value):
        patch_calls.append((param, value))
        return True

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {"sbs": {"THRESHOLD_READS": [100, 200, 400]}},
            "sweep_tiles": {"n_tiles": 3},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={"n_plates": 1}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 50,
            "phase_breakdown": {p: 5 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "select_tiles_one_per_well", side_effect=tiles_stub),
        patch.object(ap, "sweep_headless", return_value=sweep_df),
        patch.object(ap, "pick_sweep_winner", return_value=200),
        patch.object(ap, "patch_marker_block_value", side_effect=patch_stub),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "save_interview"),
        patch.object(ap, "is_active", return_value=False),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase", return_value=0),
        patch.object(ap, "build_phase_qc", side_effect=lambda *a, **kw: _make_qc_summary(a[1])),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify"),
        patch.object(ap, "mark_phase_done"),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert rc == 0, f"Expected 0, got {rc}"

    # C1: select_validation_tiles_from_metadata must have been called for sbs
    sbs_tile_calls = [c for c in tiles_called if c[1] == "sbs"]
    assert sbs_tile_calls, "per-well fan-out (select_tiles_one_per_well) was not called for sbs"

    # I1: patch_marker_block_value must have been called with the sweep winner
    winner_patches = [c for c in patch_calls if c[0] == "THRESHOLD_READS" and c[1] == repr(200)]
    assert winner_patches, (
        f"patch_marker_block_value not called with winner 200 for THRESHOLD_READS; "
        f"patch_calls={patch_calls}"
    )


# ---------------------------------------------------------------------------
# C1 regression: fresh screen (no brieflow_output/) must not return RC4
# ---------------------------------------------------------------------------


def test_run_fresh_screen_passes_disk_check(ap, tmp_path):
    """On a fresh screen, brieflow_output/ does not exist yet.

    The conductor must fall back to analysis_dir for the disk check so
    shutil.disk_usage does not raise OSError and return RC4 before preprocess
    even runs.  check_disk_available is NOT mocked here — the real function
    runs against the tmp filesystem, which always has enough free space for
    the zero-GB phase_breakdown values returned by the mocked
    compute_disk_requirements.
    """
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    # Deliberately do NOT create brieflow_output/ to simulate a fresh screen.

    run_calls = []
    notify_calls = []

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        # check_disk_available intentionally NOT patched — real function must work
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=100),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase",
                     side_effect=lambda a, mods, tag, notes: run_calls.append(mods[0]) or 0),
        patch.object(ap, "build_phase_qc",
                     side_effect=lambda *a, **kw: _make_qc_summary(a[1])),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))),
        patch.object(ap, "mark_phase_done"),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    # Must NOT return RC4 (disk unavailable) — the fresh-screen fallback must work.
    assert rc != 4, (
        "run_screen returned RC4 (disk unavailable) on a fresh screen with no "
        "brieflow_output/ directory. The disk check must fall back to analysis_dir."
    )
    # The loop must have advanced past the disk check and called run_one_phase.
    assert run_calls, (
        "run_one_phase was never called — the conductor stalled before the first phase. "
        f"Return code was {rc}."
    )


# ---------------------------------------------------------------------------
# New: notebook_run_failure → RC5, pause+notify, run_one_phase NOT called
# ---------------------------------------------------------------------------


def test_notebook_run_failure_triggers_pause(ap, tmp_path):
    """run_phase_notebook failing on preprocess → RC5, action_needed, run_one_phase not called."""
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    notify_calls = []
    run_calls = []
    set_paused_calls = []

    nb_fail = {"ok": False, "returncode": 1,
               "log": tmp_path / ".brieflow" / "preprocess.notebook.log"}

    def nb_stub(analysis_dir, phase, conda_env, **kw):
        if phase == "preprocess":
            return nb_fail
        return _NB_OK

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=100),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", side_effect=nb_stub),
        patch.object(ap, "run_one_phase",
                     side_effect=lambda a, mods, tag, notes: run_calls.append(mods[0]) or 0),
        patch.object(ap, "build_phase_qc",
                     side_effect=lambda *a, **kw: _make_qc_summary(a[1])),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))),
        patch.object(ap, "mark_phase_done"),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused",
                     side_effect=lambda d, ph, reason: set_paused_calls.append((ph, reason))),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert rc == 5, f"Expected RC5 on notebook run failure, got {rc}"

    def _is_action_needed(call):
        a, kw = call
        return kw.get("priority") == "action_needed" or (len(a) >= 4 and a[3] == "action_needed")

    assert any(_is_action_needed(c) for c in notify_calls), (
        "Expected action_needed notify on notebook run failure"
    )
    assert run_calls == [], (
        f"run_one_phase must NOT be called after notebook failure; got {run_calls}"
    )
    assert any(ph == "preprocess" for ph, _ in set_paused_calls), (
        "set_paused must be called for preprocess on notebook failure"
    )
    assert any("notebook_run_failed" in reason for _, reason in set_paused_calls), (
        "set_paused reason must contain 'notebook_run_failed'"
    )


# ---------------------------------------------------------------------------
# New: missing conda env → pause+notify (RC3), no notebook run
# ---------------------------------------------------------------------------


def test_missing_conda_env_pauses(ap, tmp_path):
    """_conda_env returning None → pause+notify with action_needed, no notebook run."""
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    notify_calls = []
    set_paused_calls = []
    nb_calls = []

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value=None),
        patch.object(ap, "run_phase_notebook",
                     side_effect=lambda *a, **kw: nb_calls.append(a) or _NB_OK),
        patch.object(ap, "run_one_phase", return_value=0),
        patch.object(ap, "build_phase_qc",
                     side_effect=lambda *a, **kw: _make_qc_summary(a[1])),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))),
        patch.object(ap, "mark_phase_done"),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused",
                     side_effect=lambda d, ph, reason: set_paused_calls.append((ph, reason))),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    # Returns RC3 (template gap) because missing conda_env is treated like a template gap
    assert rc == 3, f"Expected RC3 on missing conda env, got {rc}"

    def _is_action_needed(call):
        a, kw = call
        return kw.get("priority") == "action_needed" or (len(a) >= 4 and a[3] == "action_needed")

    assert any(_is_action_needed(c) for c in notify_calls), (
        "Expected action_needed notify on missing conda env"
    )
    assert nb_calls == [], (
        f"run_phase_notebook must NOT be called when conda env is missing; got {nb_calls}"
    )
    assert set_paused_calls, "set_paused must be called on missing conda env"


# ---------------------------------------------------------------------------
# New: notebook called before run_one_phase per phase (order guarantee)
# ---------------------------------------------------------------------------


def test_notebook_before_snakemake_per_phase(ap, tmp_path):
    """For every phase, run_phase_notebook must be called before run_one_phase."""
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()

    call_order: list[tuple[str, str]] = []  # (kind, phase)

    def nb_stub(analysis_dir, phase, conda_env, **kw):
        call_order.append(("notebook", phase))
        return _NB_OK

    def run_stub(a, mods, tag, notes):
        call_order.append(("snakemake", mods[0]))
        return 0

    patches = [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 0,
            "phase_breakdown": {p: 0 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=100),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", side_effect=nb_stub),
        patch.object(ap, "run_one_phase", side_effect=run_stub),
        patch.object(ap, "build_phase_qc",
                     side_effect=lambda *a, **kw: _make_qc_summary(a[1])),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": []}),
        patch.object(ap, "notify"),
        patch.object(ap, "mark_phase_done"),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]
    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert rc == 0

    # Verify per-phase ordering: for each phase, the notebook call must appear
    # strictly before the snakemake call in call_order.
    for phase in RUN_PHASES:
        nb_idx = next(
            (i for i, (kind, ph) in enumerate(call_order) if kind == "notebook" and ph == phase),
            None,
        )
        run_idx = next(
            (i for i, (kind, ph) in enumerate(call_order) if kind == "snakemake" and ph == phase),
            None,
        )
        assert nb_idx is not None, f"run_phase_notebook not called for phase {phase}"
        assert run_idx is not None, f"run_one_phase not called for phase {phase}"
        assert nb_idx < run_idx, (
            f"Phase {phase}: run_phase_notebook (idx={nb_idx}) must come before "
            f"run_one_phase (idx={run_idx}). call_order={call_order}"
        )


def _happy_patches(ap, notify_calls, mark_done_calls):
    """The happy-path patch set, shared by the BRIEFLOW_STOP_AFTER tests."""
    return [
        patch.object(ap, "resolve_all_phases", return_value={"filled": [], "gaps": []}),
        patch.object(ap, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(ap, "input_layout_check", return_value=0),
        patch.object(ap, "load_lab_defaults", return_value={
            "autonomy_mode": "unattended",
            "sweep_bands": {"sbs": {"THRESHOLD_READS": [100, 200]}},
            "sweep_tiles": {"n_tiles": 2},
            "recovery": {"max_attempts": 3},
        }),
        patch.object(ap, "probe_geometry", return_value={"n_plates": 1}),
        patch.object(ap, "compute_disk_requirements", return_value={
            "required_gb": 100,
            "phase_breakdown": {p: 10 for p in PHASES},
        }),
        patch.object(ap, "check_disk_available", return_value={"ok": True}),
        patch.object(ap, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(ap, "pick_sweep_winner", return_value=200),
        patch.object(ap, "patch_marker_block_value", return_value=True),
        patch.object(ap, "record_tuned_confirmed"),
        patch.object(ap, "load_interview", return_value={"params": []}),
        patch.object(ap, "_conda_env", return_value="brieflow_test"),
        patch.object(ap, "run_phase_notebook", return_value=_NB_OK),
        patch.object(ap, "run_one_phase", return_value=0),
        patch.object(ap, "build_phase_qc", return_value=_make_qc_summary("sbs", pass_=True)),
        patch.object(ap, "qc_gate_verdict", return_value={"pass": True, "failures": [], "rationale": "ok"}),
        patch.object(ap, "notify", side_effect=lambda *a, **kw: notify_calls.append((a, kw))),
        patch.object(ap, "mark_phase_done", side_effect=lambda d, ph: mark_done_calls.append(ph)),
        patch.object(ap, "phase_done", return_value=False),
        patch.object(ap, "set_paused"),
    ]


def test_stop_after_bounds_the_chain(ap, tmp_path, monkeypatch):
    """BRIEFLOW_STOP_AFTER=merge runs through merge and skips aggregate/cluster."""
    monkeypatch.setenv("BRIEFLOW_STOP_AFTER", "merge")
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()
    notify_calls, mark_done_calls = [], []

    rc = _run_with_patches(
        lambda: ap.run_screen(args), ap, _happy_patches(ap, notify_calls, mark_done_calls)
    )

    assert rc == 0
    assert mark_done_calls == ["preprocess", "sbs", "phenotype", "merge"]
    assert "aggregate" not in mark_done_calls and "cluster" not in mark_done_calls
    messages = [a[2] for (a, kw) in notify_calls if len(a) >= 3]
    assert any("STOPPING AFTER merge" in m for m in messages), messages
    assert not any("ALL PHASES COMPLETE" in m for m in messages), messages


def test_stop_after_unknown_phase_is_an_error(ap, tmp_path, monkeypatch):
    """A typo must fail loudly rather than silently running the whole chain."""
    monkeypatch.setenv("BRIEFLOW_STOP_AFTER", "merrge")
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()
    notify_calls, mark_done_calls = [], []

    rc = _run_with_patches(
        lambda: ap.run_screen(args), ap, _happy_patches(ap, notify_calls, mark_done_calls)
    )

    assert rc != 0
    assert mark_done_calls == []
    messages = [a[2] for (a, kw) in notify_calls if len(a) >= 3]
    assert any("not a phase" in m for m in messages), messages


def test_qc_advisory_continues_past_gate_failure(ap, tmp_path, monkeypatch):
    """A phase listed in BRIEFLOW_QC_ADVISORY reports its gate failure but does not pause."""
    monkeypatch.setenv("BRIEFLOW_QC_ADVISORY", "phenotype")
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()
    notify_calls, mark_done_calls = [], []
    patches = [p for p in _happy_patches(ap, notify_calls, mark_done_calls)
               if getattr(p, "attribute", None) not in ("qc_gate_verdict", "build_phase_qc")]
    # the summary must carry the phase so the verdict can fail for exactly one of them
    patches.append(patch.object(ap, "build_phase_qc",
                                side_effect=lambda root, phase, **kw: _make_qc_summary(phase, pass_=True)))
    patches.append(patch.object(ap, "qc_gate_verdict", side_effect=lambda s: {
        "pass": s.phase != "phenotype",
        "failures": ["cell_retention"], "rationale": "cell_retention 0.28 < 0.75"}))

    rc = _run_with_patches(lambda: ap.run_screen(args), ap, patches)

    assert rc == 0
    assert set(mark_done_calls) == set(PHASES), mark_done_calls
    messages = [a[2] for (a, kw) in notify_calls if len(a) >= 3]
    assert any("ADVISORY" in m for m in messages), messages


def test_qc_advisory_pass_is_not_reported_as_qc_ok(ap, tmp_path, monkeypatch):
    """An accepted advisory failure must not read as 'PASS (QC ok)' in the run log."""
    monkeypatch.setenv("BRIEFLOW_QC_ADVISORY", "phenotype")
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()
    notify_calls, mark_done_calls = [], []
    patches = [p for p in _happy_patches(ap, notify_calls, mark_done_calls)
               if getattr(p, "attribute", None) not in ("qc_gate_verdict", "build_phase_qc")]
    patches.append(patch.object(ap, "build_phase_qc",
                                side_effect=lambda root, phase, **kw: _make_qc_summary(phase, pass_=True)))
    patches.append(patch.object(ap, "qc_gate_verdict", side_effect=lambda s: {
        "pass": s.phase != "phenotype",
        "failures": ["cell_retention"], "rationale": "cell_retention 0.28 < 0.75"}))

    assert _run_with_patches(lambda: ap.run_screen(args), ap, patches) == 0
    messages = [a[2] for (a, kw) in notify_calls if len(a) >= 3]
    assert not any(m.startswith("phase phenotype: PASS (QC ok)") for m in messages), messages
    assert any("phase phenotype: PASS (QC gate failed, accepted as advisory)" in m
               for m in messages), messages
    assert any(m.startswith("phase sbs: PASS (QC ok)") for m in messages), messages


def test_qc_warn_is_not_reported_as_qc_ok(ap, tmp_path):
    """A passing phase with warn metrics names them instead of reading 'QC ok'."""
    from brieflow_qc import Metric

    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()
    notify_calls, mark_done_calls = [], []

    def summary(root, phase, **kw):
        s = _make_qc_summary(phase, pass_=True)
        if phase == "merge":
            s.metrics.append(Metric(name="sbs_recovery", value=0.513, status="warn"))
        return s

    patches = [p for p in _happy_patches(ap, notify_calls, mark_done_calls)
               if getattr(p, "attribute", None) != "build_phase_qc"]
    patches.append(patch.object(ap, "build_phase_qc", side_effect=summary))

    assert _run_with_patches(lambda: ap.run_screen(args), ap, patches) == 0
    messages = [a[2] for (a, kw) in notify_calls if len(a) >= 3]
    assert any(m.startswith("phase merge: PASS (QC warn: sbs_recovery)") for m in messages)
    assert any(m.startswith("phase sbs: PASS (QC ok)") for m in messages), messages


@pytest.mark.parametrize("value,runs_notebook", [(None, False), (False, False), (True, True)])
def test_classify_runs_only_when_run_classify_is_true(ap, tmp_path, value, runs_notebook):
    """RUN_CLASSIFY=False was ignored: 7_classify.py ran and raised (no trained model)."""
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()
    (tmp_path / ".brieflow" / "interview.json").write_text("{}")
    notify_calls, mark_done_calls = [], []
    iv = {"params": [{"notebook": "classify", "param_name": "RUN_CLASSIFY", "value": value}]}
    patches = [p for p in _happy_patches(ap, notify_calls, mark_done_calls)
               if getattr(p, "attribute", None) not in ("load_interview", "run_phase_notebook")]
    nb = MagicMock(return_value=_NB_OK)
    patches += [patch.object(ap, "load_interview", return_value=iv),
                patch.object(ap, "run_phase_notebook", nb)]

    assert _run_with_patches(lambda: ap.run_screen(args), ap, patches) == 0
    assert "classify" in mark_done_calls
    ran = [c.args[1] for c in nb.call_args_list]
    assert ("classify" in ran) is runs_notebook, ran


def test_finished_phase_with_unchanged_config_is_gated_not_resubmitted(ap, tmp_path):
    """Re-entering a phase whose run finished (e.g. after a QC pause) re-ran it in full."""
    args = _make_args(tmp_path)
    args.from_phase = "sbs"  # an upstream phase that ran later would make the marker stale
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yml").write_text("sbs: {threshold_reads: 400}\n")
    ap.write_phase_marker(tmp_path, "sbs", "run_1", "config/config.yml")
    notify_calls, mark_done_calls = [], []
    patches = [p for p in _happy_patches(ap, notify_calls, mark_done_calls)
               if getattr(p, "attribute", None) != "run_one_phase"]
    run = MagicMock(return_value=0)
    patches.append(patch.object(ap, "run_one_phase", run))

    assert _run_with_patches(lambda: ap.run_screen(args), ap, patches) == 0
    ran = [c.args[1] for c in run.call_args_list]
    assert ["sbs"] not in ran and ["phenotype"] in ran
    assert "sbs" in mark_done_calls
    messages = [a[2] for (a, kw) in notify_calls if len(a) >= 3]
    assert any("sbs: outputs complete from run run_1" in m for m in messages), messages
    assert ap.read_phase_marker(tmp_path, "phenotype")["config_sha256"]


def test_qc_advisory_unknown_phase_is_an_error(ap, tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEFLOW_QC_ADVISORY", "phenotpye")
    args = _make_args(tmp_path)
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "brieflow_output").mkdir()
    notify_calls, mark_done_calls = [], []
    rc = _run_with_patches(lambda: ap.run_screen(args), ap,
                           _happy_patches(ap, notify_calls, mark_done_calls))
    assert rc != 0
    assert mark_done_calls == []
