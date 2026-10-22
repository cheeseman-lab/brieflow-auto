"""Range-semantics tests (A4) — --from / --through / --only and the env fallback.

Tests:
  - resolve_phase_range is pure: slices PHASES for each flag combination.
  - BRIEFLOW_STOP_AFTER is the fallback for --through; the flag wins.
  - an unknown env phase raises ValueError.
  - --only sbs runs exactly sbs and marks exactly sbs done.
  - --from phenotype leaves earlier phases unrun + unmarked, journals one range-skip.
  - --through merge stops after merge; --through classify stops on a notebook-only phase.
  - the F1 launch gate stops an unapproved run at RC8 before any hook; --approved on a
    screen with no review, or with a review the range has since changed, still stops;
    --approved against the review the previous invocation wrote journals and runs;
    a gate that could not be imported refuses the run rather than running it ungated.

Fixtures are deliberately self-contained (each test file loads the module
independently); do not import them across test files.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
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
    """Isolate from an exported BRIEFLOW_STOP_AFTER. Autouse (not inline in
    _run_happy) so a test that sets the env var in its own body still wins."""
    monkeypatch.delenv("BRIEFLOW_STOP_AFTER", raising=False)


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
    captured callbacks once run_screen has returned. The `_fake` marker keeps
    _run_happy from patching its own MagicMock over this routing."""

    def get_hooks(name):
        return fake

    get_hooks._fake = True
    monkeypatch.setattr(rn, "get_hooks", get_hooks)
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
    rn, args, calls, monkeypatch, notifies=None, run_one_phase_rc=0, gaps=(), patch_gate=True,
    screen_yaml=None,
):
    """Run run_screen under the happy-path patch list, recording run_one_phase calls
    into `calls` and, when a list is passed, every notify call into `notifies`.

    `run_one_phase_rc` is what run_one_phase returns; `gaps` is the gap list
    resolve_all_phases reports; `patch_gate` stubs the F1 launch gate to approved
    (pass False to exercise the real review + --approved path).

    Progress state (mark_phase_done / phase_done) is left real so the tests can
    read .brieflow/progress.json back."""
    analysis_dir = Path(args.analysis_dir)
    (analysis_dir / ".brieflow").mkdir(exist_ok=True)
    (analysis_dir / "brieflow_output").mkdir(exist_ok=True)

    patches = [
        patch.object(rn, "resolve_all_phases", return_value={"filled": [], "gaps": list(gaps)}),
        patch.object(
            rn, "screen_yaml_check", return_value=screen_yaml or {"parses": True, "missing": []}
        ),
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
            return_value={"pass": True, "failures": [], "rationale": "ok"},
        ),
        patch.object(
            rn,
            "notify",
            side_effect=lambda *a, **k: notifies.append((a, k)) if notifies is not None else None,
        ),
        patch.object(rn, "notify_selftest"),
    ]
    # Landed in later tasks (A6 manifest, A5 hooks) — patch if present. A fake
    # already routed through _install keeps its routing.
    for name, kw in (
        ("write_run_manifest", {"return_value": None}),
        ("get_hooks", {"return_value": MagicMock()}),
    ):
        target = getattr(rn, name, None)
        if target is not None and not getattr(target, "_fake", False):
            patches.append(patch.object(rn, name, **kw))
    if patch_gate:
        patches.append(patch.object(rn, "write_launch_review", return_value={"review_sha256": "x"}))
        patches.append(patch.object(rn, "launch_approved", return_value=True))

    rc = _run_with_patches(lambda: rn.run_screen(args), rn, patches)
    while _REGISTERED:
        _REGISTERED.pop(0)()
    return rc


def _notify_texts(notifies) -> list[str]:
    return [a[2] for (a, k) in notifies if len(a) >= 3]


def _done_phases(analysis_dir: Path) -> list[str]:
    return json.loads((analysis_dir / ".brieflow" / "progress.json").read_text())["done_phases"]


# ---------------------------------------------------------------------------
# resolve_phase_range: pure slicing
# ---------------------------------------------------------------------------


def test_resolve_phase_range_pure(rn):
    def ns(**k):
        return argparse.Namespace(**{"from_phase": None, "through": None, "only": None, **k})

    assert rn.resolve_phase_range(ns()) == rn.PHASES
    assert rn.resolve_phase_range(ns(only="sbs")) == ["sbs"]
    assert (
        rn.resolve_phase_range(ns(from_phase="phenotype"))
        == rn.PHASES[rn.PHASES.index("phenotype") :]
    )
    assert rn.resolve_phase_range(ns(through="merge")) == rn.PHASES[: rn.PHASES.index("merge") + 1]


def test_env_fallback_and_flag_wins(rn, monkeypatch):
    ns = argparse.Namespace(from_phase=None, through=None, only=None)
    monkeypatch.setenv("BRIEFLOW_STOP_AFTER", "merge")
    assert rn.resolve_phase_range(ns)[-1] == "merge"
    ns.through = "sbs"
    assert rn.resolve_phase_range(ns)[-1] == "sbs"


def test_env_unknown_phase_is_error(rn, monkeypatch):
    monkeypatch.setenv("BRIEFLOW_STOP_AFTER", "nope")
    with pytest.raises(ValueError):
        rn.resolve_phase_range(argparse.Namespace(from_phase=None, through=None, only=None))


def test_unknown_only_on_a_bare_namespace_is_an_error(rn):
    """argparse validates the flags, so only a non-argparse caller reaches this —
    but it must be a named ValueError, not an IndexError several lines later."""
    with pytest.raises(ValueError, match="--only"):
        rn.resolve_phase_range(argparse.Namespace(from_phase=None, through=None, only="nope"))


def test_empty_range_is_an_error(rn, monkeypatch):
    """--from after the bound (e.g. --from aggregate under an exported
    BRIEFLOW_STOP_AFTER=merge) is an empty slice, not a silent no-op."""
    monkeypatch.setenv("BRIEFLOW_STOP_AFTER", "merge")
    with pytest.raises(ValueError, match="empty phase range"):
        rn.resolve_phase_range(argparse.Namespace(from_phase="aggregate", through=None, only=None))


def test_range_bound_source_names_the_flag(rn, monkeypatch):
    def ns(**k):
        return argparse.Namespace(**{"from_phase": None, "through": None, "only": None, **k})

    assert rn.range_bound_source(ns()) is None
    assert rn.range_bound_source(ns(from_phase="sbs")) is None
    assert rn.range_bound_source(ns(only="sbs")) == "--only"
    assert rn.range_bound_source(ns(through="merge")) == "--through"
    monkeypatch.setenv("BRIEFLOW_STOP_AFTER", "merge")
    assert rn.range_bound_source(ns()) == "BRIEFLOW_STOP_AFTER"
    assert rn.range_bound_source(ns(through="sbs")) == "--through"


# ---------------------------------------------------------------------------
# run_screen: the range applied to the loop
# ---------------------------------------------------------------------------


def test_only_sbs_runs_exactly_sbs(rn, tmp_path, monkeypatch):
    calls = []
    args = _make_args(tmp_path)
    args.only = "sbs"
    rc = _run_happy(rn, args, calls, monkeypatch)
    assert rc == 0 and [c[1] for c in calls] == [["sbs"]]
    assert _done_phases(tmp_path) == ["sbs"]


def test_from_phenotype_skips_without_marking_done_and_journals(rn, tmp_path, monkeypatch):
    calls, decisions = [], []
    monkeypatch.setattr(rn, "record_decision", lambda *a, **k: decisions.append(k))
    args = _make_args(tmp_path)
    args.from_phase = "phenotype"
    rc = _run_happy(rn, args, calls, monkeypatch)
    assert rc == 0
    done = _done_phases(tmp_path)
    assert "preprocess" not in done and "sbs" not in done
    assert sum(1 for d in decisions if d["kind"] == "range-skip") == 1


def test_through_merge_stops_after_merge(rn, tmp_path, monkeypatch):
    calls, notifies = [], []
    args = _make_args(tmp_path)
    args.through = "merge"
    rc = _run_happy(rn, args, calls, monkeypatch, notifies)
    assert rc == 0 and [c[1] for c in calls][-1] == ["merge"]
    assert "aggregate" not in _done_phases(tmp_path)
    assert any("STOPPING AFTER merge (--through)" in m for m in _notify_texts(notifies))


def test_env_bound_names_the_env_var_in_the_stop_notify(rn, tmp_path, monkeypatch):
    calls, notifies = [], []
    monkeypatch.setenv("BRIEFLOW_STOP_AFTER", "merge")
    rc = _run_happy(rn, _make_args(tmp_path), calls, monkeypatch, notifies)
    assert rc == 0 and [c[1] for c in calls][-1] == ["merge"]
    assert any("STOPPING AFTER merge (BRIEFLOW_STOP_AFTER)" in m for m in _notify_texts(notifies))


def test_from_after_the_bound_pauses_instead_of_crashing(rn, tmp_path, monkeypatch):
    """--from cluster under BRIEFLOW_STOP_AFTER=sbs is empty: notify + pause +
    _RC_TEMPLATE_GAP, never an IndexError on phases[0]."""
    calls, notifies = [], []
    monkeypatch.setenv("BRIEFLOW_STOP_AFTER", "sbs")
    args = _make_args(tmp_path)
    args.from_phase = "cluster"
    rc = _run_happy(rn, args, calls, monkeypatch, notifies)
    assert rc == rn._RC_TEMPLATE_GAP
    assert calls == []
    assert any(k.get("priority") == "action_needed" for (a, k) in notifies)
    assert any("empty phase range" in m for m in _notify_texts(notifies))


def test_middle_slice_runs_exactly_the_slice(rn, tmp_path, monkeypatch):
    calls, decisions = [], []
    monkeypatch.setattr(rn, "record_decision", lambda *a, **k: decisions.append(k))
    args = _make_args(tmp_path)
    args.from_phase, args.through = "sbs", "merge"
    rc = _run_happy(rn, args, calls, monkeypatch)
    assert rc == 0 and [c[1] for c in calls] == [["sbs"], ["phenotype"], ["merge"]]
    skips = [d for d in decisions if d["kind"] == "range-skip"]
    assert len(skips) == 1 and skips[0]["inputs"]["skipped"] == ["preprocess"]


def test_through_notebook_only_phase(rn, tmp_path, monkeypatch):
    calls = []
    args = _make_args(tmp_path)
    args.through = "classify"
    rc = _run_happy(rn, args, calls, monkeypatch)
    assert rc == 0 and "classify" in _done_phases(tmp_path)


# ---------------------------------------------------------------------------
# run_screen: the F1 launch gate
# ---------------------------------------------------------------------------


def _hermetic_review(rn, monkeypatch):
    """Render the review without its interview section or a compute probe: the real
    _interview_html reaches for brieflow_wizard, which needs the screen env, and
    probe_compute shells out to sinfo. _interview_html is patched on the module object
    the runner holds, not on a name the runner does not read."""
    monkeypatch.setattr(rn._launch_review, "_interview_html", lambda iv: "<p>iv</p>")
    monkeypatch.setattr(rn, "probe_compute", lambda: {"backend": "local"})


def _review_on_disk(analysis_dir: Path) -> dict:
    return json.loads((analysis_dir / ".brieflow" / "launch_review.json").read_text())


def test_gate_stops_with_8_and_calls_no_hooks(rn, tmp_path, monkeypatch):
    """An unapproved run writes the review and stops before any data moves: RC8,
    no phase run, and teardown as the only hook (RC8 is not an escalating exit)."""
    _hermetic_review(rn, monkeypatch)
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    a = _make_args(tmp_path)
    a.approved = False
    calls = []
    rc = _run_happy(rn, a, calls, monkeypatch, patch_gate=False)
    assert rc == rn._RC_AWAITING_LAUNCH and calls == [] and fake.calls == ["teardown"]
    assert (tmp_path / ".brieflow" / "launch_review.html").exists()


def test_gate_stops_when_no_review_was_ever_written(rn, tmp_path, monkeypatch):
    """--approved on a virgin screen is not an approval — there is nothing the operator
    could have read. The review is written, and the run still stops."""
    _hermetic_review(rn, monkeypatch)
    a = _make_args(tmp_path)
    a.approved = True
    calls = []
    assert _run_happy(rn, a, calls, monkeypatch, patch_gate=False) == rn._RC_AWAITING_LAUNCH
    assert calls == []
    assert _review_on_disk(tmp_path)["review_sha256"]


def test_gate_reopens_when_the_range_changes(rn, tmp_path, monkeypatch):
    """The review on disk described --only sbs; this run is a different range, so the
    standing approval does not carry and the file is replaced with the new one."""
    _hermetic_review(rn, monkeypatch)
    a = _make_args(tmp_path)
    a.only = "sbs"
    assert _run_happy(rn, a, [], monkeypatch, patch_gate=False) == rn._RC_AWAITING_LAUNCH
    reviewed = _review_on_disk(tmp_path)

    b = _make_args(tmp_path)
    b.from_phase, b.through, b.approved = "sbs", "phenotype", True
    calls = []
    assert _run_happy(rn, b, calls, monkeypatch, patch_gate=False) == rn._RC_AWAITING_LAUNCH
    assert calls == []
    now = _review_on_disk(tmp_path)
    assert now["review_sha256"] != reviewed["review_sha256"]
    assert now["what_will_run"]["phases"] == ["sbs", "phenotype"]


def test_gate_approved_journals_and_runs(rn, tmp_path, monkeypatch):
    """--approved against the review the PREVIOUS invocation wrote runs the chain and
    journals the approval as an operator decision."""
    _hermetic_review(rn, monkeypatch)
    decisions = []
    monkeypatch.setattr(rn, "record_decision", lambda *a, **k: decisions.append(k))
    a = _make_args(tmp_path)
    a.approved = False
    assert _run_happy(rn, a, [], monkeypatch, patch_gate=False) == rn._RC_AWAITING_LAUNCH
    a.approved = True
    assert _run_happy(rn, a, [], monkeypatch, patch_gate=False) == 0
    assert any(d["kind"] == "launch-approved" and d["source"] == "operator" for d in decisions)


def test_gaps_are_reviewed_before_the_run_stops(rn, tmp_path, monkeypatch):
    """An unresolved interview row stops the run at RC3 — and the operator still gets
    the review that names what is missing, which is the only place the gap list is
    written down. No data moves: teardown is the only hook."""
    _hermetic_review(rn, monkeypatch)
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    calls = []
    rc = _run_happy(
        rn,
        _make_args(tmp_path),
        calls,
        monkeypatch,
        gaps=[{"param_name": "DET_RANGE"}],
        patch_gate=False,
    )
    assert rc == rn._RC_TEMPLATE_GAP
    assert calls == [] and fake.calls == ["teardown"]
    assert _review_on_disk(tmp_path)["operator_gaps"] == [{"param_name": "DET_RANGE"}]
    assert "DET_RANGE" in (tmp_path / ".brieflow" / "launch_review.html").read_text()


def test_a_blank_screen_yaml_stops_before_the_interview(rn, tmp_path, monkeypatch):
    """A required screen.yaml fact left blank stops at RC3 and names the field; nothing is
    resolved, reviewed or run."""
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    calls, notifies = [], []
    rc = _run_happy(
        rn,
        _make_args(tmp_path),
        calls,
        monkeypatch,
        notifies=notifies,
        screen_yaml={"parses": True, "missing": ["library.detection_target"]},
    )
    assert rc == rn._RC_TEMPLATE_GAP and calls == []
    assert any("library.detection_target" in str(n) for n in notifies)
    assert not (tmp_path / ".brieflow" / "launch_review.html").exists()


def test_a_review_that_cannot_be_written_still_returns_3(rn, tmp_path, monkeypatch):
    """The gap return code is the finding; the review is a courtesy on top of it and
    must not be able to turn a gap into a traceback."""
    _hermetic_review(rn, monkeypatch)
    monkeypatch.setattr(rn, "write_launch_review", MagicMock(side_effect=RuntimeError("boom")))
    calls = []
    rc = _run_happy(
        rn, _make_args(tmp_path), calls, monkeypatch, gaps=[{"param_name": "DET_RANGE"}]
    )
    assert rc == rn._RC_TEMPLATE_GAP and calls == []


def test_range_skip_is_journalled_only_once_the_run_is_approved(rn, tmp_path, monkeypatch):
    """A run that stops at the gate never started, so it skipped nothing. The
    range-skip decision belongs after the approval, beside it in the journal."""
    _hermetic_review(rn, monkeypatch)
    decisions = []
    monkeypatch.setattr(rn, "record_decision", lambda *a, **k: decisions.append(k))
    a = _make_args(tmp_path)
    a.from_phase, a.approved = "phenotype", False
    assert _run_happy(rn, a, [], monkeypatch, patch_gate=False) == rn._RC_AWAITING_LAUNCH
    assert [d for d in decisions if d["kind"] == "range-skip"] == []

    a.approved = True
    assert _run_happy(rn, a, [], monkeypatch, patch_gate=False) == 0
    assert len([d for d in decisions if d["kind"] == "range-skip"]) == 1


def test_gate_unimportable_refuses_to_run(rn, tmp_path, monkeypatch):
    """The gate fails closed. With brieflow_launch_review unimportable there is no
    review to read and no approval to check, so the run stops at RC8 — running
    ungated is the one outcome the gate exists to prevent."""
    fake = FakeHooks()
    _install(rn, monkeypatch, fake)
    monkeypatch.setattr(rn, "write_launch_review", None)
    calls, notifies = [], []
    rc = _run_happy(rn, _make_args(tmp_path), calls, monkeypatch, notifies, patch_gate=False)
    assert rc == rn._RC_AWAITING_LAUNCH
    assert calls == [] and fake.calls == ["teardown"]
    assert any("launch review unavailable" in m for m in _notify_texts(notifies))
    assert any(k.get("priority") == "action_needed" for (a, k) in notifies)
