"""Launch-gate tests (F1) — .brieflow/launch_review.{html,json} + --approved (spec §4.3).

Tests:
  - the review JSON carries all six sections; the HTML shows the interview and the
    re-run command, and the JSON's hash is the one the writer returned.
  - operator gaps render above the interview table.
  - --approved absent is never approved; --approved against the hash a previous
    invocation left on disk is.
  - a virgin screen has no prior hash; a review superseded by a changed range is not
    an approval of the new one.
  - the hash moves with the phase range and with the backend.
  - the re-run command names the resolved analysis dir, carries an interpreter that
    `python` on PATH does not resolve to, and is shell-quoted.
  - the HTML is written before the JSON approval token.

Fixtures are deliberately self-contained (each test file loads the module
independently); do not import them across test files.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_launch_review.py"


@pytest.fixture(scope="module")
def lr():
    spec = importlib.util.spec_from_file_location("brieflow_launch_review", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_launch_review"] = m
    spec.loader.exec_module(m)
    return m


IV = {
    "params": [
        {
            "param_name": "THRESHOLD_READS",
            "notebook": "2_sbs.py",
            "value": 50,
            "value_source": "sweep-seed",
            "active": True,
        }
    ]
}


def _args(**k):
    b = dict(
        backend="local",
        lifecycle_hooks="noop",
        screen_id=None,
        approved=False,
        from_phase=None,
        through=None,
        only=None,
    )
    b.update(k)
    return argparse.Namespace(**b)


def _write(lr, tmp_path, args, gaps=(), phases=("sbs", "phenotype")):
    (tmp_path / ".brieflow").mkdir(exist_ok=True)
    return lr.write_launch_review(
        tmp_path,
        args,
        "t",
        interview=IV,
        phases=list(phases),
        geometry={"plates": 1, "wells": 4, "tiles_per_well": 100},
        disk={"phase_breakdown": {"sbs": 10.0}},
        disk_check_path=tmp_path,
        compute={"backend": "local"},
        lab={"sweep_tiles": {"max_tiles": 10}},
        gaps=list(gaps),
    )


def test_review_written_with_sections(lr, tmp_path):
    rev = _write(lr, tmp_path, _args())
    js = json.loads((tmp_path / ".brieflow" / "launch_review.json").read_text())
    html = (tmp_path / ".brieflow" / "launch_review.html").read_text()
    assert js["schema"] == "brieflow-launch-review/v1"
    assert js["review_sha256"] == rev["review_sha256"]
    for key in (
        "what_will_run",
        "operator_gaps",
        "interview",
        "geometry_and_disk",
        "sweeps_planned",
        "how_to_proceed",
    ):
        assert key in js
    assert "THRESHOLD_READS" in html and "--approved" in html
    assert "sbs" in js["what_will_run"]["phases"]


def test_gaps_listed_first(lr, tmp_path):
    js = _write(lr, tmp_path, _args(), gaps=[{"param_name": "DET_RANGE"}])
    assert js["operator_gaps"] == [{"param_name": "DET_RANGE"}]
    html = (tmp_path / ".brieflow" / "launch_review.html").read_text()
    assert html.index("DET_RANGE") < html.index("THRESHOLD_READS")


_IV_DOC = (
    '<!doctype html><html><head><meta charset="utf-8"><title>Interview review</title>'
    "<style>.badges span{border-radius:12px}</style></head>"
    "<body><h1>Interview review</h1><table><tr><td>IV_ROW</td></tr></table></body></html>"
)


def _fake_wizard(monkeypatch, doc=_IV_DOC):
    """Stand in for brieflow_wizard: the real renderer needs a resolvable interview,
    and what is under test is what the launch page does with a WHOLE document."""
    import types

    mod = types.ModuleType("brieflow_wizard")
    mod.interview_review = lambda iv, *a, **k: {"rows": []}
    mod.render_interview_review_html = lambda review: doc
    monkeypatch.setitem(sys.modules, "brieflow_wizard", mod)


def test_interview_section_is_not_a_nested_document(lr, tmp_path, monkeypatch):
    """The wizard renders a whole page; the launch review embeds it as a section. Two
    <!doctype>/<html> pairs in one file is not a document any browser parses as meant,
    so the section carries the inner <body> only and the rules move to the head."""
    _fake_wizard(monkeypatch)
    _write(lr, tmp_path, _args())
    html = (tmp_path / ".brieflow" / "launch_review.html").read_text()
    assert html.count("<!doctype") == 1
    assert html.count("<html") == 1 and html.count("<body") == 1
    assert "IV_ROW" in html
    # The interview's own rules survive the hoist, ahead of the page's own.
    assert ".badges span{border-radius:12px}" in html
    assert html.index(".badges span") < html.index("</head>")


def test_interview_section_falls_back_to_a_plain_table(lr, tmp_path, monkeypatch):
    """No brieflow env, no wizard: the section is still a param/value/source table,
    and still a fragment."""
    import types

    broken = types.ModuleType("brieflow_wizard")
    monkeypatch.setitem(sys.modules, "brieflow_wizard", broken)
    _write(lr, tmp_path, _args())
    html = (tmp_path / ".brieflow" / "launch_review.html").read_text()
    assert html.count("<!doctype") == 1 and html.count("<body") == 1
    assert "THRESHOLD_READS" in html and "sweep-seed" in html


def test_prior_hash_is_the_previous_invocation(lr, tmp_path):
    assert lr.read_prior_review_hash(tmp_path) is None
    first = _write(lr, tmp_path, _args())
    assert lr.read_prior_review_hash(tmp_path) == first["review_sha256"]


def test_not_approved_without_flag(lr, tmp_path):
    rev = _write(lr, tmp_path, _args())
    assert lr.launch_approved(_args(approved=False), rev, rev["review_sha256"]) is False


def test_approved_against_the_prior_review(lr, tmp_path):
    _write(lr, tmp_path, _args())
    prior = lr.read_prior_review_hash(tmp_path)
    fresh = _write(lr, tmp_path, _args())
    assert lr.launch_approved(_args(approved=True), fresh, prior) is True


def test_no_prior_review_is_not_approved(lr, tmp_path):
    """A virgin screen: --approved cannot approve a page nobody has been shown."""
    rev = _write(lr, tmp_path, _args())
    assert lr.launch_approved(_args(approved=True), rev, None) is False


def test_superseded_review_not_approved(lr, tmp_path):
    """The file on disk describes the range the operator read; this run's range changed,
    so the fresh review is a different one and has to be read before it is approved."""
    _write(lr, tmp_path, _args())
    prior = lr.read_prior_review_hash(tmp_path)
    changed = _write(lr, tmp_path, _args(), phases=["sbs"])
    assert lr.launch_approved(_args(approved=True), changed, prior) is False
    assert lr.read_prior_review_hash(tmp_path) == changed["review_sha256"]


def test_hash_changes_with_range_and_backend(lr):
    a = lr.review_hash(IV, ["sbs"], "local", "noop")
    assert (
        a
        != lr.review_hash(IV, ["sbs", "phenotype"], "local", "noop")
        != lr.review_hash(IV, ["sbs"], "slurm", "noop")
    )


def test_write_survives_an_unwritable_dir(lr, tmp_path):
    """A review that cannot be written is not an exception at launch — it is a run
    that stays unapproved, since launch_approved reads the file back."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory")
    rev = lr.write_launch_review(
        blocked,
        _args(),
        "t",
        interview=IV,
        phases=["sbs"],
        geometry={},
        disk={},
        disk_check_path=tmp_path,
        compute={},
        lab={},
        gaps=[],
    )
    assert rev["review_sha256"]
    assert lr.read_prior_review_hash(blocked) is None
    assert lr.launch_approved(_args(approved=True), rev, None) is False


def test_command_names_the_resolved_analysis_dir(lr, tmp_path, monkeypatch):
    """--analysis-dir comes from the resolved path in scope, not the argv token: the
    printed command must work from anywhere, whatever the operator typed."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["/x/scripts/brieflow_run.py", "--screen", "--analysis-dir", "../foo", "--only", "sbs"],
    )
    cmd = _write(lr, tmp_path, _args())["how_to_proceed"]["command"]
    assert f"--analysis-dir {tmp_path}" in cmd
    assert "../foo" not in cmd and "--screen" not in cmd
    assert cmd.endswith("--only sbs --approved")


def test_command_carries_an_interpreter_python_does_not_resolve_to(lr, tmp_path, monkeypatch):
    """PYTHON= is what makes the pasted command run in the right env, so the test is
    whether `python` on PATH IS this interpreter — not whether it is called python."""
    monkeypatch.setattr(sys, "argv", ["/x/scripts/brieflow_run.py", "--screen"])
    monkeypatch.setattr(lr.shutil, "which", lambda name: "/usr/bin/python")
    monkeypatch.setattr(sys, "executable", "/opt/envs/brieflow_x/bin/python3.11")
    cmd = _write(lr, tmp_path, _args())["how_to_proceed"]["command"]
    assert cmd.startswith("PYTHON=/opt/envs/brieflow_x/bin/python3.11 bin/brieflow-run ")
    monkeypatch.setattr(sys, "executable", "/usr/bin/python")
    fresh = _write(lr, tmp_path, _args())["how_to_proceed"]["command"]
    assert fresh.startswith("bin/brieflow-run ")
    monkeypatch.setattr(lr.shutil, "which", lambda name: None)
    bare = _write(lr, tmp_path, _args())["how_to_proceed"]["command"]
    assert bare.startswith("PYTHON=/usr/bin/python bin/brieflow-run ")


def test_the_env_python_on_path_needs_no_prefix(lr, tmp_path, monkeypatch):
    """A conda env's `python` is a symlink to its own python3.11: the same interpreter
    by resolved path, so the operator is not asked to carry PYTHON= for nothing."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "python3.11"
    exe.write_text("#!/bin/sh\n")
    (bindir / "python").symlink_to(exe)
    monkeypatch.setattr(sys, "argv", ["/x/scripts/brieflow_run.py", "--screen"])
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(lr.shutil, "which", lambda name: str(bindir / "python"))
    cmd = _write(lr, tmp_path, _args())["how_to_proceed"]["command"]
    assert cmd.startswith("bin/brieflow-run ")


def test_command_is_shell_quoted(lr, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/x/scripts/brieflow_run.py", "--screen"])
    spaced = tmp_path / "my screen-analysis"
    spaced.mkdir()
    cmd = _write(lr, spaced, _args())["how_to_proceed"]["command"]
    assert f"'{spaced}'" in cmd


def test_html_is_written_before_the_approval_token(lr, tmp_path, monkeypatch):
    """The JSON is the cross-invocation approval token: a disk-full between the two
    writes must not leave a fresh token beside the page the operator last read."""
    (tmp_path / ".brieflow").mkdir(exist_ok=True)
    real, written = Path.write_text, []

    def flaky(self, *a, **k):
        written.append(self.name)
        if len(written) > 1:
            raise OSError("No space left on device")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", flaky)
    lr.write_launch_review(
        tmp_path,
        _args(),
        "t",
        interview=IV,
        phases=["sbs"],
        geometry={},
        disk={},
        disk_check_path=tmp_path,
        compute={},
        lab={},
        gaps=[],
    )
    assert written[0] == lr.REVIEW_HTML
    assert not (tmp_path / ".brieflow" / lr.REVIEW_JSON).exists()
