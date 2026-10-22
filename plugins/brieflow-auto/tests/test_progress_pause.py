import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runner.py"
spec = importlib.util.spec_from_file_location("brieflow_runner", SCRIPT)
runner = importlib.util.module_from_spec(spec)
sys.modules.setdefault("brieflow_runner", runner)
spec.loader.exec_module(runner)


def test_later_completed_phase_clears_a_stale_pause(tmp_path):
    (tmp_path / ".brieflow").mkdir()
    runner.set_paused(tmp_path, "classify", "notebook_run_failed")
    runner.mark_phase_done(tmp_path, "aggregate")
    progress = json.loads((tmp_path / ".brieflow" / "progress.json").read_text())
    assert "paused" not in progress and progress["done_phases"] == ["aggregate"]
    journal = (tmp_path / ".brieflow" / "decisions.jsonl").read_text()
    assert "notebook_run_failed" in journal
