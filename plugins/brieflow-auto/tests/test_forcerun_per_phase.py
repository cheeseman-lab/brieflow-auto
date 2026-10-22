"""--forcerun reaches only the phases that define the rule; Snakemake fails on an unknown rule."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from brieflow_runloop import forcerun_for_modules  # noqa: E402


def test_forcerun_filtered_to_phase_rules(tmp_path):
    rules = tmp_path / "brieflow" / "workflow" / "rules"
    rules.mkdir(parents=True)
    (rules / "aggregate.smk").write_text("rule split_datasets:\n    input: x\n\nrule filter:\n")
    (rules / "cluster.smk").write_text("rule phate_leiden_clustering:\n")
    analysis = tmp_path / "analysis"
    analysis.mkdir()
    assert forcerun_for_modules(analysis, ["aggregate"], ["split_datasets"]) == ["split_datasets"]
    assert forcerun_for_modules(analysis, ["cluster"], ["split_datasets"]) == []
    assert forcerun_for_modules(analysis, ["mozzarellm"], ["split_datasets"]) == ["split_datasets"]
