"""Phases that passed with warn metrics, or whose failure was let through, read as plain
passes at the end of a run. Their QC outcome is kept and listed at completion."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runner.py"
spec = importlib.util.spec_from_file_location("brieflow_runner", SCRIPT)
runner = importlib.util.module_from_spec(spec)
sys.modules.setdefault("brieflow_runner", runner)
spec.loader.exec_module(runner)


def _summary(**statuses):
    return SimpleNamespace(
        overall_status="warn",
        metrics=[SimpleNamespace(name=k, status=v) for k, v in statuses.items()],
    )


def test_review_note_lists_warn_advisory_and_accepted(tmp_path):
    runner.record_qc_status(tmp_path, "sbs", _summary(single_map_rate="pass"))
    runner.record_qc_status(tmp_path, "merge", _summary(sbs_recovery="warn"))
    runner.record_qc_status(
        tmp_path, "phenotype", _summary(cell_retention="fail", final_cells="info"), advisory=True
    )
    runner.record_qc_status(tmp_path, "cluster", _summary(best_recall="fail"))
    runner.record_qc_status(tmp_path, "cluster", accepted=True)
    note = runner.qc_review_note(tmp_path)
    assert note == (
        "phenotype fail advisory (cell_retention); merge warn (sbs_recovery); "
        "cluster fail accepted (best_recall)"
    )
    assert runner.qc_review_note(tmp_path / "none") == ""
