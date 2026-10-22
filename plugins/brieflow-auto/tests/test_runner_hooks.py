"""Lifecycle-hook tests (A5) — the four hook points around run_screen's phase loop.

Tests:
  - a full run calls pre → post → teardown, in that order.
  - a bounded (--through) run still counts as success: pre → post → teardown.
  - a failing phase calls pre → failure → teardown, never post.
  - a pre-launch return (template gap) calls only teardown — never pre or failure.
  - a raising on_pre_phase_1 pauses the run without calling failure.
  - with no hooks flag the default NoopHooks leaves loop behaviour unchanged.
  - an escalating exit writes .brieflow/issue_draft.md and journals it, from the
    stashed recover action when there is one and from the paused phase when there
    is not; a stash belonging to another run — including one whose tag merely shares
    a prefix — is discarded, not drafted from.
  - a failing on_pre_phase_1 drafts too: inputs that never arrived are a defect,
    unlike the pre-launch operator gaps above it.

Fixtures are deliberately self-contained (each test file loads the module
independently); do not import them across test files.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runner.py"


@pytest.fixture(scope="module")
def rn():
    spec = importlib.util.spec_from_file_location("brieflow_runner", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_runner"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def _stub_cluster_final(rn, monkeypatch):
    """Cluster FINAL_LEIDEN_RESOLUTIONS selection is covered in test_runner.py.
    Here stub it so the loop reaches/completes the cluster phase deterministically
    (it otherwise runs an extra notebook pass + needs a parseable table)."""
    monkeypatch.setattr(rn, "resolve_cluster_final", lambda *a, **k: [11])


@pytest.fixture(autouse=True)
def _no_stop_after_env(monkeypatch):
    """Isolate from an exported BRIEFLOW_STOP_AFTER / BRIEFLOW_QC_ADVISORY. Autouse
    (not inline in _run_happy) so a test that sets one in its own body still wins."""
    monkeypatch.delenv("BRIEFLOW_STOP_AFTER", raising=False)
    monkeypatch.delenv("BRIEFLOW_QC_ADVISORY", raising=False)


@pytest.fixture(autouse=True)
def _stub_preflight(rn, monkeypatch):
    """Preflight runs the real brieflow_preflight.py subprocess (needs a brieflow
    submodule + config). Default it to clean (0) so loop tests reach the run step."""
    monkeypatch.setattr(rn, "run_preflight", lambda *a, **k: 0)


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
        from_phase=None,
        through=None,
        only=None,
        lifecycle_hooks="noop",
        screen_id=None,
        approved=True,
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
    return pd.DataFrame(
        {
            "value": [100, 200, 400],
            "mean_single_rate": [0.5, 0.7, 0.6],
            "std_single_rate": [0.05, 0.03, 0.04],
            "n_tiles": [3, 3, 3],
            "qc_pass_frac": [1.0, 1.0, 1.0],
        }
    )


_NB_OK = {"ok": True, "returncode": 0, "log": Path("/dev/null")}

# atexit registrations captured by _install, drained by _run_happy after run_screen.
_REGISTERED: list = []


class FakeHooks:
    name = "fake"

    def __init__(self):
        self.calls = []

    def on_pre_phase_1(self, d, s):
        self.calls.append("pre")

    def on_post_terminal_phase(self, d, s):
        self.calls.append("post")

    def on_failure(self, d, s, diag):
        self.calls.append("failure")

    def on_teardown(self, d, s):
        self.calls.append("teardown")


def _install(rn, monkeypatch, fake):
    """Route get_hooks at `fake` and capture atexit registrations instead of running
    them. Registration precedes the pre hook in run_screen, so running the teardown
    at registration time would order the calls wrongly; _run_happy drains the
    captured callbacks once run_screen has returned."""
    monkeypatch.setattr(rn, "get_hooks", lambda name: fake)
    _REGISTERED.clear()
    fake.registered = _REGISTERED
    monkeypatch.setattr(rn.atexit, "register", lambda fn: _REGISTERED.append(fn))


def _run_with_patches(func, rn_module, patch_specs):
    """Apply a list of patch.object specs via ExitStack, run func, return result."""
    with ExitStack() as stack:
        for p in patch_specs:
            stack.enter_context(p)
        return func()


def _run_happy(
    rn,
    args,
    calls,
    monkeypatch,
    notifies=None,
    run_one_phase_rc=0,
    gaps=(),
    patch_gate=True,
    qc_pass=True,
):
    """Run run_screen under the happy-path patch list, recording run_one_phase calls
    into `calls` and, when a list is passed, every notify call into `notifies`.

    `run_one_phase_rc` is what run_one_phase returns; `qc_pass` is the gate verdict;
    `gaps` is the gap list resolve_all_phases reports. `patch_gate` stubs the F1
    launch gate to approved;
    pass False to exercise the real review + --approved path. get_hooks is left
    unpatched here (unlike the copy in test_runner_range.py) because these tests
    route it through _install.

    Progress state (mark_phase_done / phase_done) is left real so the tests can
    read .brieflow/progress.json back."""
    analysis_dir = Path(args.analysis_dir)
    (analysis_dir / ".brieflow").mkdir(exist_ok=True)
    (analysis_dir / "brieflow_output").mkdir(exist_ok=True)

    patches = [
        patch.object(rn, "resolve_all_phases", return_value={"filled": [], "gaps": list(gaps)}),
        patch.object(rn, "screen_yaml_check", return_value={"parses": True, "missing": []}),
        patch.object(rn, "input_layout_check", return_value=0),
        patch.object(
            rn,
            "load_lab_defaults",
            return_value={
                "autonomy_mode": "unattended",
                "sweep_bands": {},
                "sweep_tiles": {"n_tiles": 2},
                "recovery": {"max_attempts": 3},
            },
        ),
        patch.object(rn, "probe_geometry", return_value={}),
        patch.object(rn, "compute_disk_requirements", return_value={"phase_breakdown": {}}),
        patch.object(rn, "check_disk_available", return_value={"ok": True}),
        patch.object(rn, "sweep_headless", return_value=_make_sweep_df()),
        patch.object(rn, "pick_sweep_winner", return_value=100),
        patch.object(rn, "patch_marker_block_value", return_value=True),
        patch.object(rn, "record_tuned_confirmed"),
        patch.object(rn, "load_interview", return_value={"params": []}),
        patch.object(rn, "_conda_env", return_value="env"),
        patch.object(rn, "run_phase_notebook", return_value=_NB_OK),
        patch.object(
            rn,
            "run_one_phase",
            side_effect=lambda a, phases, tag, notes: (
                calls.append((a, phases, tag, notes)),
                run_one_phase_rc,
            )[1],
        ),
        patch.object(
            rn, "build_phase_qc", side_effect=lambda out, phase, **k: _make_qc_summary(phase)
        ),
        patch.object(
            rn,
            "qc_gate_verdict",
            return_value={"pass": qc_pass, "failures": [], "rationale": "ok"},
        ),
        patch.object(
            rn,
            "notify",
            side_effect=lambda *a, **k: notifies.append((a, k)) if notifies is not None else None,
        ),
        patch.object(rn, "notify_selftest"),
    ]
    # Landed in a later task (A6 manifest) — patch if present.
    if getattr(rn, "write_run_manifest", None) is not None:
        patches.append(patch.object(rn, "write_run_manifest", return_value=None))
    if patch_gate:
        patches.append(patch.object(rn, "write_launch_review", return_value={"review_sha256": "x"}))
        patches.append(patch.object(rn, "launch_approved", return_value=True))

    rc = _run_with_patches(lambda: rn.run_screen(args), rn, patches)
    while _REGISTERED:
        _REGISTERED.pop(0)()
    return rc


def test_full_run_order(rn, tmp_path, monkeypatch):
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    assert _run_happy(rn, _make_args(tmp_path), [], monkeypatch) == 0
    assert fake.calls == ["pre", "post", "teardown"]


def test_through_run_pushes(rn, tmp_path, monkeypatch):
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    a = _make_args(tmp_path)
    a.through = "merge"
    assert _run_happy(rn, a, [], monkeypatch) == 0
    assert fake.calls == ["pre", "post", "teardown"]


def test_paused_run_calls_failure(rn, tmp_path, monkeypatch):
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    rc = _run_happy(rn, _make_args(tmp_path), [], monkeypatch, run_one_phase_rc=1)
    assert rc == rn._RC_RUN_FAIL and fake.calls == ["pre", "failure", "teardown"]


def test_pre_launch_gap_calls_nothing(rn, tmp_path, monkeypatch):
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    rc = _run_happy(rn, _make_args(tmp_path), [], monkeypatch, gaps=[{"param_name": "X"}])
    assert rc == rn._RC_TEMPLATE_GAP and fake.calls == ["teardown"]


def test_pre_hook_failure_pauses(rn, tmp_path, monkeypatch):
    fake = FakeHooks()
    fake.on_pre_phase_1 = lambda d, s: (_ for _ in ()).throw(RuntimeError("no bucket"))
    _install(rn, monkeypatch, fake)
    calls = []
    rc = _run_happy(rn, _make_args(tmp_path), calls, monkeypatch)
    assert rc == rn._RC_RUN_FAIL and "failure" not in fake.calls
    assert calls == []


def test_noop_default_matches_previous_behaviour(rn, tmp_path, monkeypatch):
    """No get_hooks patch: the real NoopHooks runs, and the loop behaves exactly as
    it does in test_runner_loop's happy path."""
    calls = []
    assert _run_happy(rn, _make_args(tmp_path), calls, monkeypatch) == 0
    assert [c[1] for c in calls] == [[p] for p in rn.PHASES if p not in rn.NOTEBOOK_ONLY_PHASES]


def test_unknown_qc_advisory_calls_nothing(rn, tmp_path, monkeypatch):
    """An unknown BRIEFLOW_QC_ADVISORY phase is a pre-launch config error: it must be
    caught before the pre hook, not after it (which would pull inputs and then archive
    a failure for a run that never started)."""
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    monkeypatch.setenv("BRIEFLOW_QC_ADVISORY", "nope")
    calls = []
    rc = _run_happy(rn, _make_args(tmp_path), calls, monkeypatch)
    assert rc == rn._RC_TEMPLATE_GAP and fake.calls == ["teardown"]
    assert calls == []


def test_latest_diag_bundle_is_tag_scoped_and_newest_first(rn, tmp_path):
    """The failure hook archives THIS run's newest bundle. Lexicographic order across
    every run's bundles would hand GcsHooks another tag's file."""
    assert rn._latest_diag_bundle(tmp_path, "run_test") is None

    d = tmp_path / "diag"
    d.mkdir()
    # Named so lexicographic order disagrees with mtime order in both directions.
    older, newer, other = d / "run_test_z.txt", d / "run_test_a.txt", d / "other_tag_a.txt"
    for f in (older, newer, other):
        f.write_text("x")
    os.utime(older, (1, 1000))
    os.utime(newer, (1, 2000))
    os.utime(other, (1, 3000))  # newest overall, but belongs to another run

    assert rn._latest_diag_bundle(tmp_path, "run_test") == newer
    assert rn._latest_diag_bundle(tmp_path, "no_such_tag") == other


def test_escalating_exit_writes_issue_draft_and_journals(rn, tmp_path, monkeypatch):
    """The run gave up: leave the operator a draft built from the recover action THIS
    run stashed (tag = phase_tag(args.tag, ...)), and consume the stash so the next run
    cannot draft from a stale one."""
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    decisions = []
    monkeypatch.setattr(rn, "record_decision", lambda *a, **k: decisions.append(k))
    (tmp_path / ".brieflow").mkdir(exist_ok=True)
    (tmp_path / ".brieflow" / "last_escalation.json").write_text(
        json.dumps(
            {
                "tag": "run_test__p1_sbs",
                "phase": "sbs",
                "action": {
                    "classification": "unknown",
                    "rule": "r",
                    "reason": "why",
                    "should_file_issue": True,
                },
                "diag_bundle": "d.txt",
            }
        )
    )
    rc = _run_happy(rn, _make_args(tmp_path), [], monkeypatch, run_one_phase_rc=1)
    assert rc == rn._RC_RUN_FAIL
    draft = (tmp_path / ".brieflow" / "issue_draft.md").read_text()
    assert "sbs" in draft and "why" in draft and "d.txt" in draft
    assert not (tmp_path / ".brieflow" / "last_escalation.json").exists()
    drafted = [d for d in decisions if d["kind"] == "escalate-draft"]
    assert len(drafted) == 1 and drafted[0]["phase"] == "sbs" and drafted[0]["why"] == "why"


def test_unclassified_exit_drafts_from_the_paused_phase(rn, tmp_path, monkeypatch):
    """A failure the recover loop never classified (a QC gate, a preflight) leaves no
    stash — the draft still names the phase the runner paused at."""
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    decisions = []
    monkeypatch.setattr(rn, "record_decision", lambda *a, **k: decisions.append(k))
    rc = _run_happy(rn, _make_args(tmp_path), [], monkeypatch, run_one_phase_rc=1)
    assert rc == rn._RC_RUN_FAIL
    drafted = [d for d in decisions if d["kind"] == "escalate-draft"]
    assert len(drafted) == 1 and drafted[0]["phase"] == rn.PHASES[0]
    assert f"rc={rn._RC_RUN_FAIL}" == drafted[0]["why"]
    assert rn.PHASES[0] in (tmp_path / ".brieflow" / "issue_draft.md").read_text()


def test_a_stash_from_another_run_is_discarded(rn, tmp_path, monkeypatch):
    """A direct /brieflow-module run stashes an escalation too. Drafting this failure
    from that one would name a rule this run never touched, so the stash is taken only
    when its tag belongs to this run — and removed either way."""
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    (tmp_path / ".brieflow").mkdir(exist_ok=True)
    (tmp_path / ".brieflow" / "last_escalation.json").write_text(
        json.dumps(
            {
                "tag": "other__p0_sbs",
                "phase": "sbs",
                "action": {
                    "rule": "not_this_run",
                    "reason": "someone else",
                    "should_file_issue": True,
                },
                "diag_bundle": "other.txt",
            }
        )
    )
    rc = _run_happy(rn, _make_args(tmp_path), [], monkeypatch, qc_pass=False)
    assert rc == rn._RC_QC_GATE_FAIL
    draft = json.loads((tmp_path / ".brieflow" / "issue_draft.json").read_text())
    assert draft["classification"] is None and draft["suggested_repo"] is None
    assert draft["phase"] == rn.PHASES[0] and draft["diag_bundle"] is None
    assert not (tmp_path / ".brieflow" / "last_escalation.json").exists()


def test_a_stash_whose_tag_only_shares_a_prefix_is_discarded(rn, tmp_path, monkeypatch):
    """run_10's stash is not run_1's. The tag test is anchored to the separator
    phase_tag puts in, so the tenth run of a screen cannot feed the first one's draft."""
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    args = _make_args(tmp_path)
    args.tag = "run_1"
    (tmp_path / ".brieflow").mkdir(exist_ok=True)
    (tmp_path / ".brieflow" / "last_escalation.json").write_text(
        json.dumps(
            {
                "tag": "run_10__p0_sbs",
                "phase": "sbs",
                "action": {
                    "rule": "not_this_run",
                    "reason": "another run",
                    "should_file_issue": True,
                },
                "diag_bundle": "other.txt",
            }
        )
    )
    rc = _run_happy(rn, args, [], monkeypatch, qc_pass=False)
    assert rc == rn._RC_QC_GATE_FAIL
    draft = json.loads((tmp_path / ".brieflow" / "issue_draft.json").read_text())
    assert draft["classification"] is None and draft["suggested_repo"] is None
    assert draft["phase"] == rn.PHASES[0] and draft["diag_bundle"] is None
    assert not (tmp_path / ".brieflow" / "last_escalation.json").exists()


def test_pre_hook_failure_drafts_from_pre_launch(rn, tmp_path, monkeypatch):
    """Inputs that never arrived are a defect worth a draft, unlike the operator gaps
    that return above the hook."""
    fake = FakeHooks()
    fake.on_pre_phase_1 = lambda d, s: (_ for _ in ()).throw(RuntimeError("no bucket"))
    _install(rn, monkeypatch, fake)
    rc = _run_happy(rn, _make_args(tmp_path), [], monkeypatch)
    assert rc == rn._RC_RUN_FAIL
    draft = json.loads((tmp_path / ".brieflow" / "issue_draft.json").read_text())
    assert draft["phase"] == "pre-launch" and draft["trigger"] == "RUN_FAIL"


def test_pre_launch_operator_gap_drafts_nothing(rn, tmp_path, monkeypatch):
    """A template gap is an unanswered question, not a defect: no draft to file."""
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    rc = _run_happy(rn, _make_args(tmp_path), [], monkeypatch, gaps=[{"param_name": "X"}])
    assert rc == rn._RC_TEMPLATE_GAP
    assert not (tmp_path / ".brieflow" / "issue_draft.json").exists()
