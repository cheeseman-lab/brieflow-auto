import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import brieflow_manifest as bm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "brieflow_manifest.py"


def test_window_starts_at_the_launch_not_midnight(tmp_path):
    (tmp_path / ".brieflow").mkdir()
    launch = "2026-09-27T23:37:12+00:00"
    (tmp_path / ".brieflow" / "run_manifest.json").write_text(json.dumps({"written_at": launch}))
    expected = datetime.fromisoformat(launch).astimezone().strftime("%Y-%m-%dT%H:%M:%S")
    assert bm.sacct_start(tmp_path) == expected
    out = subprocess.run([sys.executable, str(SCRIPT), "--analysis-dir", str(tmp_path),
                          "--sacct-start"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == expected


def test_without_manifest_falls_back_to_recent_hours(tmp_path):
    start = datetime.strptime(bm.sacct_start(tmp_path, fallback_hours=48), "%Y-%m-%dT%H:%M:%S")
    assert abs(datetime.now() - timedelta(hours=48) - start) < timedelta(minutes=5)


def test_status_skill_does_not_cut_the_window_at_midnight_or_miss_oom():
    text = (ROOT / "skills" / "brieflow-status" / "SKILL.md").read_text()
    assert "--starttime=$START" in text and "--state=FAILED --starttime=today" not in text
    assert "--sacct-start" in text and "OUT_OF_MEMORY,TIMEOUT" in text
