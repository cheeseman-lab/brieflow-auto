"""Values the runner writes during a run (test tiles, sweep winners) changed the review hash,
so each relaunch needed a new review (6 of 12 invocations exited 8 on one screen)."""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_launch_review.py"
spec = importlib.util.spec_from_file_location("brieflow_launch_review", SCRIPT)
lr = importlib.util.module_from_spec(spec)
sys.modules.setdefault("brieflow_launch_review", lr)
spec.loader.exec_module(lr)


def _iv(test_tile, threshold, align):
    return {"params": [
        {"notebook": "phenotype", "param_name": "TEST_TILE", "value": test_tile,
         "value_source": "auto-probe"},
        {"notebook": "sbs", "param_name": "THRESHOLD_READS", "value": threshold,
         "value_source": "tuned-confirmed"},
        {"notebook": "phenotype", "param_name": "ALIGN", "value": align,
         "value_source": "operator"},
    ]}


def test_runner_written_values_do_not_change_the_hash():
    base = lr.review_hash(_iv(None, None, False), ["sbs"], "slurm", "noop")
    assert lr.review_hash(_iv(409, 400, False), ["sbs"], "slurm", "noop") == base


def test_operator_values_and_row_sources_still_do():
    base = lr.review_hash(_iv(409, 400, False), ["sbs"], "slurm", "noop")
    assert lr.review_hash(_iv(409, 400, True), ["sbs"], "slurm", "noop") != base
    moved = _iv(409, 400, False)
    moved["params"][0]["value_source"] = "operator"
    assert lr.review_hash(moved, ["sbs"], "slurm", "noop") != base
