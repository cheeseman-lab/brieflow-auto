"""Accepting a failed QC gate re-ran the whole phenotype phase (443.9 min, same metrics).
`--accept-qc <phase>` journals the decision and marks the phase done without a rerun."""

import importlib.util
import json
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_runner.py"
spec = importlib.util.spec_from_file_location("brieflow_runner", SCRIPT)
runner = importlib.util.module_from_spec(spec)
sys.modules.setdefault("brieflow_runner", runner)
spec.loader.exec_module(runner)


def _screen(tmp_path):
    (tmp_path / ".brieflow").mkdir()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yml").write_text("phenotype:\n  cell_diameter: 94\n")
    return tmp_path


def _journal(tmp_path):
    lines = (tmp_path / ".brieflow" / "decisions.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_nothing_to_accept_without_a_finished_run(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "notify", lambda *a, **k: None)
    _screen(tmp_path)
    assert runner.accept_qc(tmp_path, "t", "phenotype") == runner._RC_TEMPLATE_GAP
    assert not runner.phase_done(tmp_path, "phenotype")


def test_accept_marks_done_and_journals_the_operator_decision(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "notify", lambda *a, **k: None)
    _screen(tmp_path)
    runner.write_phase_marker(tmp_path, "phenotype", "run_1", "config/config.yml")
    runner.set_paused(tmp_path, "phenotype", "phenotype: QC gate failed — cell_retention 0.709")
    assert runner.accept_qc(tmp_path, "run_2", "phenotype") == 0
    assert runner.phase_done(tmp_path, "phenotype")
    entry = _journal(tmp_path)[-1]
    assert entry["kind"] == "qc-accept" and entry["source"] == "operator"
    assert "cell_retention 0.709" in entry["why"]
    assert entry["inputs"]["marker"]["tag"] == "run_1"


def test_reuse_needs_unchanged_config_and_no_later_upstream(tmp_path, monkeypatch):
    _screen(tmp_path)
    monkeypatch.setitem(sys.modules, "brieflow_dryrun_preview",
                        type(sys)("stub"))
    sys.modules["brieflow_dryrun_preview"].rerun_reasons = lambda *a, **k: {
        "pending": {"align_phenotype": 4380},
        "reasons": {"params have changed since last execution": ["align_phenotype"]},
        "list_changes": {"params": {"count": 4380, "examples": []},
                         "code": {"count": 0, "examples": []}},
    }
    marker = runner.write_phase_marker(tmp_path, "phenotype", "run_1", "config/config.yml")
    # Key order and formatting are not a change.
    (tmp_path / "config" / "config.yml").write_text("phenotype: {cell_diameter: 94}\n")
    reuse, detail, _ = runner.phase_rerun_check(tmp_path, "phenotype", "config/config.yml", None, marker)
    assert reuse and "not resubmitting" in detail
    (tmp_path / "config" / "config.yml").write_text("phenotype: {cell_diameter: 60}\n")
    reuse, detail, _ = runner.phase_rerun_check(tmp_path, "phenotype", "config/config.yml", None, marker)
    assert not reuse and "config changed" in detail and "4380 job(s) pending" in detail
    assert "params have changed since last execution (1)" in detail and "params 4380" in detail
    (tmp_path / "config" / "config.yml").write_text("phenotype: {cell_diameter: 94}\n")
    runner.write_phase_marker(tmp_path, "sbs", "run_3", "config/config.yml")
    reuse, detail, _ = runner.phase_rerun_check(tmp_path, "phenotype", "config/config.yml", None, marker)
    assert not reuse and "upstream ['sbs'] completed later" in detail


def test_newer_upstream_with_nothing_pending_forces_ancient_rules(tmp_path, monkeypatch):
    rules = tmp_path / "brieflow" / "workflow" / "rules"
    rules.mkdir(parents=True)
    (rules / "cluster.smk").write_text(
        "rule clean_aggregate:\n    input:\n        ancient(X),\n    output:\n        Y,\n\n"
        "rule phate_leiden_clustering:\n    input:\n        Y,\n    output:\n        Z,\n"
    )
    analysis = tmp_path / "analysis"
    (analysis / "config").mkdir(parents=True)
    (analysis / "config" / "config.yml").write_text("cluster: {}\n")
    monkeypatch.setitem(sys.modules, "brieflow_dryrun_preview", type(sys)("stub"))
    sys.modules["brieflow_dryrun_preview"].rerun_reasons = lambda *a, **k: {"pending": {}}
    marker = runner.write_phase_marker(analysis, "cluster", "run_1", "config/config.yml")
    runner.write_phase_marker(analysis, "aggregate", "run_2", "config/config.yml")
    reuse, detail, force = runner.phase_rerun_check(
        analysis, "cluster", "config/config.yml", None, marker)
    assert not reuse and force == ["clean_aggregate"] and "ancient()" in detail
