"""A 43-unit library could not reach 20 genes/cluster at any resolution (resolution 1 gave a
median of 10), yet the runner journaled a winner "nearest target". The miss is now recorded."""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import brieflow_sweep as bs  # noqa: E402

spec = importlib.util.spec_from_file_location("brieflow_runner", SCRIPTS / "brieflow_runner.py")
runner = importlib.util.module_from_spec(spec)
sys.modules.setdefault("brieflow_runner", runner)
spec.loader.exec_module(runner)

SMALL = [{"resolution": r, "n_clusters": 0, "median_genes_per_cluster": m}
         for r, m in ((1, 10.0), (3, 6.0), (5, 4.0), (7, 3.0))]
LAB = {"cluster_selection": {"target_genes_per_cluster": 20, "genes_per_cluster_tolerance": 5}}


def test_miss_reports_the_closest_and_the_shortfall():
    miss = bs.cluster_target_miss(SMALL)
    assert miss["closest_resolution"] == 1 and miss["closest_median"] == 10.0
    assert miss["tested"] == [1, 7] and miss["largest_median"] == 10.0
    hit = SMALL + [{"resolution": 0.5, "n_clusters": 0, "median_genes_per_cluster": 18.0}]
    assert bs.cluster_target_miss(hit) is None


def test_runner_journals_a_miss_not_a_winner(tmp_path, monkeypatch, capsys):
    (tmp_path / ".brieflow").mkdir()
    log = tmp_path / "cluster.log"
    log.write_text("table")
    monkeypatch.setattr(runner, "run_phase_notebook", lambda *a: {"ok": True, "log": str(log)})
    monkeypatch.setattr(runner, "parse_resolution_table", lambda text: SMALL)
    monkeypatch.setattr(runner, "patch_marker_block_value", lambda *a: None)
    monkeypatch.setattr(runner, "record_tuned_confirmed", lambda *a, **k: None)
    monkeypatch.setattr(runner, "save_interview", lambda *a: None)
    final = runner.resolve_cluster_final(tmp_path, "env", LAB, {"params": []}, tmp_path / "iv")
    assert final == [1, 2, 3]
    entry = json.loads((tmp_path / ".brieflow/decisions.jsonl").read_text().splitlines()[-1])
    assert entry["kind"] == "sweep-target-missed"
    assert "closest: resolution 1 at a median of 10.0" in entry["why"]
    assert "smaller clusters (library-limited)" in entry["why"]
    assert "target not reached" in capsys.readouterr().out
