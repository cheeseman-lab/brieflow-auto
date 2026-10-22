"""A terminal marker left in a log from an earlier run is not success for a new attempt."""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from brieflow_failure_logs import find_terminal_marker_logs  # noqa: E402


def test_marker_in_log_older_than_attempt_is_ignored(tmp_path):
    logs = tmp_path / ".snakemake" / "log"
    logs.mkdir(parents=True)
    old = logs / "2026-09-30T063920.snakemake.log"
    old.write_text("Finished jobid: 0 (Rule: all_aggregate)\n")
    week_ago = time.time() - 7 * 86400
    os.utime(old, (week_ago, week_ago))
    started = time.time() - 60
    assert find_terminal_marker_logs(tmp_path, "aggregate") == [old]
    assert find_terminal_marker_logs(tmp_path, "aggregate", since=started) == []
    new = logs / "2026-10-06T193530.snakemake.log"
    new.write_text("Finished jobid: 0 (Rule: all_aggregate)\n")
    assert find_terminal_marker_logs(tmp_path, "aggregate", since=started) == [new]
