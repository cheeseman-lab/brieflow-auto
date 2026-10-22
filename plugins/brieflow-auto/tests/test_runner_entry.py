"""Entry point tests (T9): --screen routes to run_screen; default path unchanged.

Tests brieflow_run.py's --screen flag dispatch without actually running the pipeline.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

# The entry module is brieflow_run.py (already exists)
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_run.py"
RUNNER_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runner.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def br():
    return _load_module(SCRIPT, "brieflow_run_entry")


@pytest.fixture(scope="module")
def ap():
    return _load_module(RUNNER_SCRIPT, "brieflow_runner_entry")


def _parse(br, argv, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(br, "run_screen", lambda args: seen.setdefault("args", args) and 0)
    monkeypatch.setattr(sys, "argv", ["brieflow_run.py", "--analysis-dir", str(tmp_path), *argv])
    rc = br.main()
    return rc, seen.get("args")


# ---------------------------------------------------------------------------
# brieflow_run.py must have --screen in its argument parser
# ---------------------------------------------------------------------------


def test_brieflow_run_has_screen_flag(br, tmp_path, monkeypatch):
    """main() in brieflow_run.py must accept --screen."""
    rc, a = _parse(br, ["--screen"], tmp_path, monkeypatch)
    assert rc == 0 and a.screen is True


def test_screen_flag_routes_to_run_screen(tmp_path, monkeypatch):
    """When --screen is passed, main() calls run_screen() not the old path."""
    # We need to load brieflow_run.py and patch its run_screen symbol.
    # Reload the module in this test to avoid scope contamination.
    import importlib.util as ilu

    spec = ilu.spec_from_file_location("_br_ep_test", SCRIPT)
    m = ilu.module_from_spec(spec)
    sys.modules["_br_ep_test"] = m
    spec.loader.exec_module(m)

    run_screen_calls = []

    # Create minimal required filesystem structure
    analysis_dir = tmp_path / "myscreen-analysis"
    analysis_dir.mkdir()
    (analysis_dir / "flow.sh").write_text("#!/bin/bash\nexit 0\n")
    config_dir = analysis_dir / "config"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("output_path: brieflow_output\n")

    def fake_run_screen(args):
        run_screen_calls.append(args)
        return 0

    # Patch run_screen in the loaded module's namespace
    monkeypatch.setattr(m, "run_screen", fake_run_screen, raising=False)

    import sys as _sys
    old_argv = _sys.argv[:]
    _sys.argv = [
        "brieflow_run.py",
        "--screen",
        "--analysis-dir", str(analysis_dir),
        "--modules", "preprocess",
        "--backend", "local",
    ]
    try:
        rc = m.main()
    except SystemExit as e:
        rc = e.code
    finally:
        _sys.argv = old_argv

    assert run_screen_calls, (
        "--screen flag did not call run_screen(). "
        "Expected run_screen to be called once."
    )
    assert rc == 0


def test_default_path_does_not_call_run_screen(tmp_path, monkeypatch):
    """Without --screen, the existing pipeline path is taken, not run_screen."""
    import importlib.util as ilu

    spec = ilu.spec_from_file_location("_br_ep_default_test", SCRIPT)
    m = ilu.module_from_spec(spec)
    sys.modules["_br_ep_default_test"] = m
    spec.loader.exec_module(m)

    run_screen_calls = []

    def fake_run_screen(args):
        run_screen_calls.append(args)
        return 0

    monkeypatch.setattr(m, "run_screen", fake_run_screen, raising=False)

    # Also patch run_flow so we don't actually run anything
    def fake_run_flow(*a, **kw):
        return (Path("/dev/null"), 0.1, 0)

    def fake_check_terminal(*a, **kw):
        return (True, [])

    monkeypatch.setattr(m, "run_flow", fake_run_flow, raising=True)
    monkeypatch.setattr(m, "check_terminal_marker", fake_check_terminal, raising=True)

    # Create minimal required filesystem structure
    analysis_dir = tmp_path / "myscreen-default"
    analysis_dir.mkdir()
    (analysis_dir / "flow.sh").write_text("#!/bin/bash\nexit 0\n")
    config_dir = analysis_dir / "config"
    config_dir.mkdir()
    (config_dir / "config.yml").write_text("output_path: brieflow_output\n")

    import sys as _sys
    old_argv = _sys.argv[:]
    _sys.argv = [
        "brieflow_run.py",
        "--analysis-dir", str(analysis_dir),
        "--modules", "preprocess",
        "--backend", "local",
    ]
    try:
        m.main()
    except SystemExit:
        pass
    finally:
        _sys.argv = old_argv

    assert run_screen_calls == [], (
        "Default (non --screen) path must NOT call run_screen(). "
        f"Got calls: {run_screen_calls}"
    )


# ---------------------------------------------------------------------------
# --screen mode: retired --autopilot alias, range flags, hooks flags, --approved
# ---------------------------------------------------------------------------


def test_screen_flag_dispatches(br, tmp_path, monkeypatch):
    rc, a = _parse(br, ["--screen"], tmp_path, monkeypatch)
    assert rc == 0 and a.screen is True and a.lifecycle_hooks == "noop" and a.approved is False


def test_autopilot_alias_is_retired(br, tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as e:
        _parse(br, ["--autopilot"], tmp_path, monkeypatch)
    assert e.value.code == 2


def test_range_flags(br, tmp_path, monkeypatch):
    _, a = _parse(
        br, ["--screen", "--from", "phenotype", "--through", "merge"], tmp_path, monkeypatch
    )
    assert (a.from_phase, a.through, a.only) == ("phenotype", "merge", None)
    _, a = _parse(br, ["--screen", "--only", "sbs"], tmp_path, monkeypatch)
    assert (a.from_phase, a.through, a.only) == (None, None, "sbs")


def test_range_rejects_bad_phase_and_only_with_from(br, tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        _parse(br, ["--screen", "--only", "nope"], tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        _parse(br, ["--screen", "--only", "sbs", "--from", "sbs"], tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        _parse(br, ["--screen", "--from", "merge", "--through", "sbs"], tmp_path, monkeypatch)


def test_range_flags_require_screen(br, tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        _parse(br, ["--modules", "sbs", "--only", "sbs"], tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "argv", [["--lifecycle-hooks", "gcs"], ["--screen-id", "s1"]], ids=["hooks", "screen-id"]
)
def test_screen_only_flags_require_screen(br, tmp_path, monkeypatch, capsys, argv):
    """Hooks and the screen id are read by run_screen and by nothing else: accepting
    them on a --modules run silently ignores what the operator asked for."""
    with pytest.raises(SystemExit):
        _parse(br, ["--modules", "sbs", *argv], tmp_path, monkeypatch)
    assert "require --screen" in capsys.readouterr().err


def test_default_lifecycle_hooks_do_not_require_screen(tmp_path, monkeypatch):
    """The guard is about a NON-DEFAULT selection. `--lifecycle-hooks noop` is the
    value every run already carries, so spelling it out must not reject the run."""
    m = _load_module(SCRIPT, "_br_ep_hooks_default")
    monkeypatch.setattr(m, "run_flow", lambda *a, **kw: (Path("/dev/null"), 0.1, 0))
    monkeypatch.setattr(m, "check_terminal_marker", lambda *a, **kw: (True, []))
    analysis_dir = tmp_path / "hooks-default"
    (analysis_dir / "config").mkdir(parents=True)
    (analysis_dir / "flow.sh").write_text("#!/bin/bash\nexit 0\n")
    (analysis_dir / "config" / "config.yml").write_text("output_path: brieflow_output\n")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "brieflow_run.py",
            "--analysis-dir",
            str(analysis_dir),
            "--modules",
            "preprocess",
            "--backend",
            "local",
            "--lifecycle-hooks",
            "noop",
        ],
    )
    assert m.main() == 0


def test_hooks_and_screen_id(br, tmp_path, monkeypatch):
    _, a = _parse(
        br,
        ["--screen", "--lifecycle-hooks", "gcs", "--screen-id", "s1", "--approved"],
        tmp_path,
        monkeypatch,
    )
    assert a.lifecycle_hooks == "gcs" and a.screen_id == "s1" and a.approved is True


def test_bin_wrapper_execs_runner_with_screen(tmp_path):
    bin_path = Path(__file__).resolve().parent.parent / "bin" / "brieflow-run"
    stub = tmp_path / "py"
    stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    out = subprocess.run(
        [str(bin_path), "--only", "sbs"],
        env={**os.environ, "PYTHON": str(stub)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert out[0].endswith("scripts/brieflow_run.py") and out[1:] == ["--screen", "--only", "sbs"]
