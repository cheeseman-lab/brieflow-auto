"""One shipped watcher instead of hand-rolled ones that missed failures for ~13 h."""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_watch as bw  # noqa: E402


def _launch(tmp_path, log, progress=None, trace=None):
    bf = tmp_path / ".brieflow"
    run = bf / "traces" / "20260929T110858Z-abc"
    run.mkdir(parents=True)
    (run / "trace.json").write_text(json.dumps(trace or {"status": "running"}))
    (run / "runner.log").write_text(log)
    (bf / "progress.json").write_text(json.dumps(progress or {"done_phases": ["preprocess"]}))
    (bf / "run_manifest.json").write_text(json.dumps({"written_at": "2026-09-28T23:37:12+00:00"}))
    return tmp_path


def test_running_then_only_new_events(tmp_path):
    a = _launch(tmp_path, "[x] [info] monitor\n[x] [progress] attempt1_run: 10%\n")
    rc, lines = bw.watch(a, alive=lambda d: True, failures=lambda s: Counter())
    assert rc == bw.RC_RUNNING and "EVENT: [x] [progress] attempt1_run: 10%" in lines
    with open(a / ".brieflow/traces/20260929T110858Z-abc/runner.log", "a") as f:
        f.write("[y] [action_needed] sbs: QC gate failed\n")
    rc, lines = bw.watch(a, alive=lambda d: True, failures=lambda s: Counter())
    assert rc == bw.RC_ATTENTION
    assert [ln for ln in lines if ln.startswith("EVENT")] == [
        "EVENT: [y] [action_needed] sbs: QC gate failed"
    ]


def test_failures_are_counted_from_the_launch_across_midnight(tmp_path):
    a = _launch(tmp_path, "")
    seen = {}

    def failures(start):
        seen["start"] = start
        return Counter({("calculate_ic_sbs", "OUT_OF_MEMORY"): 21})

    rc, lines = bw.watch(a, alive=lambda d: True, failures=failures)
    assert rc == bw.RC_ATTENTION and "calculate_ic_sbs OUT_OF_MEMORY x21" in lines[-1]
    assert seen["start"].date().isoformat() in ("2026-09-28", "2026-09-29")


def test_paused_finished_and_stopped(tmp_path):
    paused = {"done_phases": [], "paused": {"phase": "phenotype", "reason": "QC gate failed"}}
    a = _launch(tmp_path / "p", "", progress=paused, trace={"status": "returned", "exit_code": 1})
    rc, lines = bw.watch(a, alive=lambda d: False, failures=lambda s: Counter())
    assert rc == bw.RC_ATTENTION and lines[0] == "STATE: paused at phenotype: QC gate failed"
    a = _launch(tmp_path / "f", "", trace={"status": "returned", "exit_code": 0})
    assert bw.watch(a, alive=lambda d: False, failures=lambda s: Counter())[0] == bw.RC_DONE
    a = _launch(tmp_path / "s", "", trace={"status": "interrupted"})
    assert bw.watch(a, alive=lambda d: False, failures=lambda s: Counter())[0] == bw.RC_STOPPED


def test_cursor_elsewhere_leaves_the_screen_untouched(tmp_path):
    a = _launch(tmp_path / "screen", "[x] [progress] attempt1_run: 10%\n")
    before = sorted(p.name for p in (a / ".brieflow").iterdir())
    cursor = tmp_path / "work" / "cursor.json"
    cursor.parent.mkdir()
    rc, lines = bw.watch(a, alive=lambda d: True, failures=lambda s: Counter(), cursor=cursor)
    assert sorted(p.name for p in (a / ".brieflow").iterdir()) == before
    assert cursor.is_file() and any(line.startswith("EVENT") for line in lines)


def test_sacct_window_runs_to_now(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return bw.subprocess.CompletedProcess(
            cmd, 0, "rule_bootstrap_gene_wildcards_all_x|OUT_OF_MEMORY\nother_job|FAILED\n", "")

    monkeypatch.setattr(bw.subprocess, "run", run)
    counts = bw.failed_jobs(bw.datetime(2026, 9, 30, 2, 39))
    assert "--endtime=now" in seen["cmd"]  # with --state, sacct's end defaults to the start
    assert counts == Counter({("bootstrap_gene", "OUT_OF_MEMORY"): 1})
