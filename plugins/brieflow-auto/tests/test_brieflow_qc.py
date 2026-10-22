"""Tests for brieflow_qc.py — post-phase QC consolidation."""
from __future__ import annotations
import importlib.util
import json
import sys
from pathlib import Path
import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_qc.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "qc"


@pytest.fixture(scope="module")
def qc():
    spec = importlib.util.spec_from_file_location("brieflow_qc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_qc"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cli_main_empty_phase_passes(qc, tmp_path, monkeypatch, capsys):
    """The /brieflow-qc CLI gates a phase from its eval/ outputs. An empty phase
    (no metrics) is 'info' -> no blocking metric -> pass -> exit 0."""
    (tmp_path / "brieflow_output").mkdir()
    monkeypatch.setattr(
        sys, "argv",
        ["brieflow_qc.py", "--analysis-dir", str(tmp_path), "--phase", "sbs", "--json"],
    )
    rc = qc.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["phase"] == "sbs" and out[0]["pass"] is True


def test_cli_main_unknown_phase_errors(qc, tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys, "argv",
        ["brieflow_qc.py", "--analysis-dir", str(tmp_path), "--phase", "bogus"],
    )
    with pytest.raises(SystemExit):
        qc.main()


def test_metric_and_summary_roundtrip(qc):
    m = qc.Metric(name="single_map_rate", value=0.62, unit="rate", level="phase",
                  threshold={"min": 0.45, "warn": 0.55}, status="pass",
                  source_file="mapping_overview.tsv")
    s = qc.QCSummary(phase="sbs", metrics=[m], overall_status="pass",
                     generated_at="2026-06-01T00:00:00Z", provenance=["mapping_overview.tsv"])
    d = s.to_dict()
    assert d["phase"] == "sbs"
    assert d["overall_status"] == "pass"
    assert d["metrics"][0]["name"] == "single_map_rate"
    assert d["metrics"][0]["value"] == 0.62
    assert d["metrics"][0]["status"] == "pass"


def test_apply_threshold_min_warn(qc):
    g = {"min": 0.45, "warn": 0.55}
    assert qc.apply_threshold(0.40, g) == "fail"   # below min
    assert qc.apply_threshold(0.50, g) == "warn"   # between min and warn
    assert qc.apply_threshold(0.60, g) == "pass"   # at/above warn
    assert qc.apply_threshold(0.45, g) == "warn"   # boundary == min is not fail
    assert qc.apply_threshold(0.55, g) == "pass"   # exactly at warn


def test_apply_threshold_max_warn(qc):
    g = {"max": 0.20, "warn": 0.10}
    assert qc.apply_threshold(0.25, g) == "fail"   # above max
    assert qc.apply_threshold(0.15, g) == "warn"   # between warn and max
    assert qc.apply_threshold(0.05, g) == "pass"   # at/below warn
    assert qc.apply_threshold(0.20, g) == "warn"   # at max boundary: > is strict, so not fail -> warn
    assert qc.apply_threshold(0.10, g) == "pass"   # exactly at warn = pass


def test_apply_threshold_none_value_and_no_gate(qc):
    assert qc.apply_threshold(None, {"min": 0.5}) == "info"
    assert qc.apply_threshold(0.5, None) == "info"


def test_apply_threshold_nan_is_info(qc):
    assert qc.apply_threshold(float("nan"), {"min": 0.45, "warn": 0.55}) == "info"


def test_overall_status_worst_wins(qc):
    assert qc.overall_status(["pass", "warn", "pass"]) == "warn"
    assert qc.overall_status(["pass", "fail", "warn"]) == "fail"
    assert qc.overall_status(["pass", "pass"]) == "pass"
    assert qc.overall_status(["info", "info"]) == "info"


def test_load_qc_gates_default(qc):
    gates = qc.load_qc_gates()  # default path = data/qc_gates.yaml
    assert "sbs" in gates
    pg = qc.phase_gates(gates, "sbs")
    assert "single_map_rate" in pg
    assert pg["single_map_rate"]["min"] == 0.25  # barcode-level gate (2026-08-28)


def test_load_qc_gates_phase(qc):
    g = qc.load_qc_gates()
    assert qc.phase_gates(g, "merge")["ph_recovery_rate"]["warn"] == 0.65
    assert qc.phase_gates(g, "nonexistent") == {}


def test_phase_gates_nested_format(qc):
    gates = {"sbs": {"phase_qc": {"single_map_rate": {"min": 0.45}}, "sweep_qc": {}}}
    assert qc.phase_gates(gates, "sbs")["single_map_rate"]["min"] == 0.45


def test_phase_gates_flat_fallback(qc):
    # Legacy flat format (phase maps directly to metric->gate) still works.
    gates = {"sbs": {"single_map_rate": {"min": 0.45}}}
    assert qc.phase_gates(gates, "sbs")["single_map_rate"]["min"] == 0.45


def test_sweep_gates(qc):
    gates = qc.load_qc_gates()
    sg = qc.sweep_gates(gates, "sbs")
    assert "cycle_dapi_shift_residual_max_px" in sg
    assert sg["cycle_dapi_shift_residual_max_px"]["max"] == 10.0
    # intra_cycle is INFO-ONLY — not present in sweep_qc
    assert "intra_cycle_channel_shift_residual_max_px" not in sg
    assert qc.sweep_gates(gates, "preprocess") == {}
    assert qc.sweep_gates(gates, "nonexistent") == {}


# ---------------------------------------------------------------------------
# Preprocess: no parquet reads — extractor absent → empty info summary
# ---------------------------------------------------------------------------

def test_preprocess_no_qc(qc, tmp_path):
    """build_phase_qc('preprocess') returns empty metrics and overall 'info'."""
    out = tmp_path / "brieflow_output"
    (out / "preprocess" / "eval").mkdir(parents=True)
    summary = qc.build_phase_qc(out, "preprocess", generated_at="2026-06-01T00:00:00Z")
    assert summary.phase == "preprocess"
    assert summary.metrics == []
    assert summary.overall_status == "info"


# ---------------------------------------------------------------------------
# SBS extractor
# ---------------------------------------------------------------------------

def test_extract_sbs_qc(qc):
    metrics = qc.extract_sbs_qc(FIXTURES / "sbs")
    by = {m.name: m for m in metrics}
    # BARCODE-level, count-weighted: (320+300)/(1000+1000) = 0.31
    assert by["single_map_rate"].value == pytest.approx(0.31)
    # gene-level is reported but NOT gated: (600+500)/2000 = 0.55
    assert by["single_gene_map_rate"].value == pytest.approx(0.55)
    assert by["single_map_rate"].unit == "rate"
    # any BARCODE: (940+900)/2000 = 0.92
    assert by["any_map_rate"].value == pytest.approx(0.92)
    assert by["any_gene_map_rate"].value == pytest.approx(0.83)
    assert by["total_cells"].value == 2000
    assert by["single_map_rate"].source_file.endswith("mapping_overview.tsv")
    # sbs seg fixture: final_cells 1000×2 / initial_cells 1080×2
    assert by["cell_retention"].value == pytest.approx(2000 / 2160, rel=1e-4)
    # nuclei_retention: final_nuclei 1000×2 / initial_nuclei 1100×2
    assert by["nuclei_retention"].value == pytest.approx(2000 / 2200, rel=1e-4)


def test_extract_sbs_qc_sums_across_plates(qc, tmp_path):
    """C1 regression: multi-plate screens must sum across all plate dirs (subdir layout)."""
    for plate, total in [("1", 1000), ("2", 2000)]:
        d = tmp_path / "eval" / "mapping" / plate
        d.mkdir(parents=True)
        rows = [
            "well\ttotal_cells__count\t1_gene_cells__count\t1_or_more_genes__count"
            "\t1_barcode_cells__count\t1_barcode_cells__percent"
            "\t1_or_more_barcodes__count\t1_or_more_barcodes__percent"
            "\t1_gene_cells__percent\t1_or_more_genes__percent",
            f"A1\t{total}\t{total // 2}\t{total - 100}\t{total // 3}\t33.0"
            f"\t{total - 200}\t80.0\t50.0\t90.0",
        ]
        (d / "mapping_overview.tsv").write_text("\n".join(rows) + "\n")
    metrics = qc.extract_sbs_qc(tmp_path)
    by = {m.name: m for m in metrics}
    assert by["total_cells"].value == 3000


_MAP_HDR = (
    "well\ttotal_cells__count\t1_barcode_cells__count\t1_barcode_cells__percent"
    "\t1_or_more_barcodes__count\t1_or_more_barcodes__percent"
    "\t1_gene_cells__count\t1_gene_cells__percent"
    "\t1_or_more_genes__count\t1_or_more_genes__percent"
)
_SEG_HDR = (
    "well\tinitial_nuclei\tinitial_cells\tafter_edge_removal_nuclei"
    "\tafter_edge_removal_cells\tfinal_cells\tfinal_nuclei"
)


def _write_map_file(path: Path, total: int, single: int, anyg: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{_MAP_HDR}\nA1\t{total}\t{single}\t{single/total*100:.1f}"
        f"\t{anyg}\t{anyg/total*100:.1f}\t{single}\t{single/total*100:.1f}"
        f"\t{anyg}\t{anyg/total*100:.1f}\n"
    )


def _write_seg_file(path: Path, init_n: int, init_c: int, final_c: int, final_n: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{_SEG_HDR}\nA1\t{init_n}\t{init_c}\t{init_n-50}\t{init_c-50}"
        f"\t{final_c}\t{final_n}\n"
    )


def test_extract_sbs_qc_prefix_layout(qc, tmp_path):
    """Prefix layout (damavand-style): P-1__mapping_overview.tsv + P-2__ in the type dir."""
    map_dir = tmp_path / "eval" / "mapping"
    seg_dir = tmp_path / "eval" / "segmentation"
    # P-1: total=1000, single=400, any=800
    _write_map_file(map_dir / "P-1__mapping_overview.tsv", 1000, 400, 800)
    # P-2: total=2000, single=1000, any=1600
    _write_map_file(map_dir / "P-2__mapping_overview.tsv", 2000, 1000, 1600)
    _write_seg_file(seg_dir / "P-1__segmentation_overview.tsv", 1100, 1000, 900, 900)
    _write_seg_file(seg_dir / "P-2__segmentation_overview.tsv", 1100, 1000, 900, 900)

    metrics = qc.extract_sbs_qc(tmp_path)
    by = {m.name: m for m in metrics}

    # count-weighted single_map_rate: (400+1000)/(1000+2000) = 1400/3000
    assert by["total_cells"].value == pytest.approx(3000)
    assert by["single_map_rate"].value == pytest.approx(1400 / 3000)
    assert by["any_map_rate"].value == pytest.approx(2400 / 3000)
    # cell_retention: (900+900)/(1000+1000) = 0.9
    assert by["cell_retention"].value == pytest.approx(0.9)


def test_extract_phenotype_qc_prefix_layout(qc, tmp_path):
    """Prefix layout phenotype segmentation: P-1__ + P-2__ directly under type dir."""
    seg_dir = tmp_path / "eval" / "segmentation"
    _write_seg_file(seg_dir / "P-1__segmentation_overview.tsv", 1100, 1000, 800, 800)
    _write_seg_file(seg_dir / "P-2__segmentation_overview.tsv", 1100, 1000, 800, 800)

    metrics = qc.extract_phenotype_qc(tmp_path)
    by = {m.name: m for m in metrics}

    # cell_retention: (800+800)/(1000+1000) = 0.8
    assert by["cell_retention"].value == pytest.approx(0.8)
    assert by["final_cells"].value == pytest.approx(1600)


# ---------------------------------------------------------------------------
# Phenotype extractor
# ---------------------------------------------------------------------------

def test_extract_phenotype_qc(qc):
    metrics = qc.extract_phenotype_qc(FIXTURES / "phenotype")
    by = {m.name: m for m in metrics}
    # final_cells/initial_cells = (800+800)/(1000+1000) = 0.8
    assert by["cell_retention"].value == pytest.approx(0.8)
    assert by["final_cells"].value == 1600
    # nuclei_retention: final_nuclei 800×2 / initial_nuclei 1100×2
    assert by["nuclei_retention"].value == pytest.approx(1600 / 2200, rel=1e-4)


# ---------------------------------------------------------------------------
# Merge / aggregate extractors
# ---------------------------------------------------------------------------

def test_extract_merge_qc(qc):
    metrics = qc.extract_merge_qc(FIXTURES / "merge")
    by = {m.name: m for m in metrics}
    # cell-weighted means: ph_recovery weighted by ph_cells (equal here) = 0.70
    assert by["ph_recovery_rate"].value == pytest.approx(0.70)
    assert by["sbs_recovery_rate"].value == pytest.approx(0.60)
    # single_gene_rate weighted by total_match_pairs (equal) = 0.45
    assert by["single_gene_rate"].value == pytest.approx(0.45)


def test_merge_partial_phenotype_coverage_passes_on_reachable_recovery(qc, tmp_path):
    """Small test: phenotype tiles cover ~1/4 of each SBS field, so SBS recovery tops out
    near ph_cells/sbs_cells (0.23) on a correct merge; the gate reads matched/min(ph, sbs)."""
    eval_dir = tmp_path / "brieflow_output" / "merge" / "eval"
    eval_dir.mkdir(parents=True)
    (eval_dir / "P-1__merge_summary.tsv").write_text(
        "well\tph_cells\tsbs_cells\tunique_ph_in_merge\tunique_sbs_in_merge\t"
        "ph_recovery_rate\tsbs_recovery_rate\ttotal_match_pairs\tsingle_gene_rate\n"
        "A1\t1623\t6963\t1554\t1554\t0.957\t0.223\t1554\t0.491\n"
        "A2\t1682\t6914\t1581\t1581\t0.94\t0.229\t1581\t0.417\n"
    )
    s = qc.build_phase_qc(tmp_path / "brieflow_output", "merge", "2026-01-01T00:00:00", config={})
    by = {m.name: m for m in s.metrics}
    assert by["recovery_of_reachable"].value == pytest.approx(3135 / 3305)
    assert by["recovery_of_reachable"].status == "pass"
    assert by["sbs_recovery_rate"].status == "info"
    assert s.overall_status != "fail"


def test_merge_collapse_still_fails_reachable_recovery(qc, tmp_path):
    eval_dir = tmp_path / "brieflow_output" / "merge" / "eval"
    eval_dir.mkdir(parents=True)
    (eval_dir / "P-1__merge_summary.tsv").write_text(
        "well\tph_cells\tsbs_cells\tunique_ph_in_merge\tunique_sbs_in_merge\t"
        "ph_recovery_rate\tsbs_recovery_rate\ttotal_match_pairs\tsingle_gene_rate\n"
        "A1\t1600\t1700\t200\t200\t0.125\t0.118\t200\t0.4\n"
    )
    s = qc.build_phase_qc(tmp_path / "brieflow_output", "merge", "2026-01-01T00:00:00", config={})
    by = {m.name: m for m in s.metrics}
    assert by["recovery_of_reachable"].status == "fail"


def test_extract_merge_qc_fallback_matching_rates_removed(qc, tmp_path):
    """Old per-well matching-rate files are no longer parsed (fallback removed).

    Screens with only *final_sbs_matching_rates.tsv / *final_phenotype_matching_rates.tsv
    and no *merge_summary.tsv now return [] — no merge QC.
    """
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()

    sbs_hdr = "well\ttotal_cells\tmatched_cells\tmatch_rate"
    (eval_dir / "P-1_W-A1__final_sbs_matching_rates.tsv").write_text(
        f"{sbs_hdr}\nA1\t1000\t876\t87.6\n"
    )
    ph_hdr = "well\ttotal_cells\tmatched_cells\tmatch_rate"
    (eval_dir / "P-1_W-A1__final_phenotype_matching_rates.tsv").write_text(
        f"{ph_hdr}\nA1\t1000\t830\t83.0\n"
    )

    metrics = qc.extract_merge_qc(tmp_path)
    assert metrics == [], f"Expected [], got {[m.name for m in metrics]}"


def test_extract_merge_qc_fallback_no_files(qc, tmp_path):
    """No eval files at all → empty list."""
    (tmp_path / "eval").mkdir()
    assert qc.extract_merge_qc(tmp_path) == []


def test_extract_aggregate_qc(qc):
    metrics = qc.extract_aggregate_qc(FIXTURES / "aggregate")
    by = {m.name: m for m in metrics}
    assert by["max_percent_na"].value == pytest.approx(0.25)
    assert by["mean_percent_na"].value == pytest.approx(0.1333, abs=1e-3)


# ---------------------------------------------------------------------------
# Cluster extractor
# ---------------------------------------------------------------------------

def _make_cluster_fixture(base: Path, combo: str, cell_class: str, res: str,
                          recall: float, cluster_sizes: list[int]) -> None:
    """Build one (combo, cell_class, res) cluster fixture directory.

    `cluster_sizes` is the per-cluster gene count list.  The fixture JSON
    records n_clusters = len(cluster_sizes) and total_genes = sum(sizes),
    so genes_per_cluster = sum(sizes)/len(sizes) = mean(sizes).
    """
    d = base / combo / cell_class / res
    d.mkdir(parents=True)
    (d / "CB-Real__global_metrics.json").write_text(
        json.dumps({"STRING": {"recall": recall}})
    )
    n = len(cluster_sizes)
    clusters = {str(i): {"size": s} for i, s in enumerate(cluster_sizes)}
    (d / "CB-Real__integrated_results.json").write_text(
        json.dumps({"total_clusters_analyzed": n, "clusters": clusters})
    )


def test_extract_cluster_qc(qc, tmp_path):
    """Two resolutions: res=5 has genes/cluster=20 (target), res=10 has 50."""
    # res=5: 4 clusters × 20 genes each = 20/cluster, recall=0.096
    _make_cluster_fixture(tmp_path, "CeCl-all_ChCo-DAPI", "all", "5",
                          recall=0.096, cluster_sizes=[20, 20, 20, 20])
    # res=10: 2 clusters × 50 genes each = 50/cluster (dist=30), recall=0.12
    _make_cluster_fixture(tmp_path, "CeCl-all_ChCo-DAPI", "all", "10",
                          recall=0.12, cluster_sizes=[50, 50])

    metrics = qc.extract_cluster_qc(tmp_path, target_genes_per_cluster=20)
    by = {m.name: m for m in metrics}

    # best_recall = max(0.096, 0.12) = 0.12
    assert by["best_recall"].value == pytest.approx(0.12)
    # nearest to target 20: res=5 gpk=20 (dist=0) vs res=10 gpk=50 (dist=30) → res=5 wins
    assert by["genes_per_cluster_at_target"].value == pytest.approx(20.0)
    assert by["recall_at_target_granularity"].value == pytest.approx(0.096)


def test_extract_cluster_qc_no_files(qc, tmp_path):
    """Returns empty list when no CB-Real__global_metrics.json found."""
    result = qc.extract_cluster_qc(tmp_path)
    assert result == []


def test_extract_cluster_qc_real_shaped(qc, tmp_path):
    """Shapes matching real reference-screen output: recall 0.096, 20 genes/cluster."""
    # res=3: 5 clusters × 4 genes → gpk=4; recall 0.04
    _make_cluster_fixture(tmp_path, "CeCl-all_ChCo-DAPI", "all", "3",
                          recall=0.04, cluster_sizes=[4, 4, 4, 4, 4])
    # res=4: 4 clusters × 5 genes → gpk=5; recall 0.05
    _make_cluster_fixture(tmp_path, "CeCl-all_ChCo-DAPI", "all", "4",
                          recall=0.05, cluster_sizes=[5, 5, 5, 5])
    # res=5: 2 clusters × 10 genes → gpk=10; recall 0.07
    _make_cluster_fixture(tmp_path, "CeCl-all_ChCo-DAPI", "all", "5",
                          recall=0.07, cluster_sizes=[10, 10])
    # res=6: 1 cluster × 20 genes → gpk=20 (dist=0 from target); recall 0.096
    _make_cluster_fixture(tmp_path, "CeCl-all_ChCo-DAPI", "all", "6",
                          recall=0.096, cluster_sizes=[20])
    # res=8: 1 cluster × 40 genes → gpk=40 (dist=20); recall 0.11 (best overall)
    _make_cluster_fixture(tmp_path, "CeCl-all_ChCo-DAPI", "all", "8",
                          recall=0.11, cluster_sizes=[40])

    metrics = qc.extract_cluster_qc(tmp_path, target_genes_per_cluster=20)
    by = {m.name: m for m in metrics}

    assert by["best_recall"].value == pytest.approx(0.11)
    # target=20: res=6 gpk=20 (dist=0) beats res=8 gpk=40 (dist=20)
    assert by["genes_per_cluster_at_target"].value == pytest.approx(20.0)
    assert by["recall_at_target_granularity"].value == pytest.approx(0.096)


# ---------------------------------------------------------------------------
# Task 9: build_phase_qc orchestrator
# ---------------------------------------------------------------------------

def test_build_phase_qc_writes_summary(qc, tmp_path):
    import shutil
    out = tmp_path / "brieflow_output"
    shutil.copytree(FIXTURES / "sbs", out / "sbs")
    summary = qc.build_phase_qc(out, "sbs", generated_at="2026-06-01T00:00:00Z")
    assert summary.phase == "sbs"
    # barcode-level single_map_rate 0.31 with gate min .25/warn .40 -> warn
    by = {m.name: m for m in summary.metrics}
    assert by["single_map_rate"].value == pytest.approx(0.31)
    assert by["single_map_rate"].status == "warn"
    assert summary.overall_status in ("pass", "warn", "fail")
    # written to disk
    written = out / "sbs" / "eval" / "qc_summary.json"
    assert written.exists()
    import json
    data = json.loads(written.read_text())
    assert data["phase"] == "sbs"


def test_build_phase_qc_phase_qc_nesting(qc, tmp_path):
    """phase_gates correctly extracts phase_qc sub-dict from nested format."""
    import shutil
    out = tmp_path / "brieflow_output"
    shutil.copytree(FIXTURES / "sbs", out / "sbs")
    # Use the default gates (nested format); verify nuclei_retention is gated
    gates = qc.load_qc_gates()
    summary = qc.build_phase_qc(out, "sbs", generated_at="t", gates=gates)
    by = {m.name: m for m in summary.metrics}
    assert "nuclei_retention" in by
    assert by["nuclei_retention"].threshold is not None


def test_build_phase_qc_unknown_phase_is_empty(qc, tmp_path):
    out = tmp_path / "brieflow_output"
    (out / "cluster" / "eval").mkdir(parents=True)
    summary = qc.build_phase_qc(out, "cluster", generated_at="2026-06-01T00:00:00Z")
    # cluster extractor returns [] when no JSONs present
    assert summary.metrics == []
    assert summary.overall_status == "info"


def test_build_phase_qc_cluster_with_lab_defaults(qc, tmp_path):
    out = tmp_path / "brieflow_output"
    _make_cluster_fixture(out / "cluster", "CeCl-all_ChCo-DAPI", "all", "5",
                          recall=0.096, cluster_sizes=[20, 20, 20, 20])
    lab_defaults = {"cluster_selection": {"target_genes_per_cluster": 20}}
    summary = qc.build_phase_qc(out, "cluster", generated_at="t",
                                 lab_defaults=lab_defaults)
    by = {m.name: m for m in summary.metrics}
    assert by["best_recall"].value == pytest.approx(0.096)


# ---------------------------------------------------------------------------
# Task 10: qc_gate_verdict
# ---------------------------------------------------------------------------

def test_qc_gate_verdict(qc):
    m_ok = qc.Metric("single_map_rate", 0.6, "rate", status="pass")
    m_bad = qc.Metric("any_map_rate", 0.4, "rate", status="fail")
    s_pass = qc.QCSummary("sbs", [m_ok], "pass", "t")
    s_fail = qc.QCSummary("sbs", [m_ok, m_bad], "fail", "t")
    assert qc.qc_gate_verdict(s_pass)["pass"] is True
    v = qc.qc_gate_verdict(s_fail)
    assert v["pass"] is False
    assert "any_map_rate" in v["failures"]
    assert v["rationale"]


def test_qc_gate_verdict_warn_passes_by_default(qc):
    s_warn = qc.QCSummary("sbs", [qc.Metric("x", 0.5, status="warn")], "warn", "t")
    assert qc.qc_gate_verdict(s_warn)["pass"] is True            # warn advances
    assert qc.qc_gate_verdict(s_warn, warn_is_fail=True)["pass"] is False


# ---------------------------------------------------------------------------
# Task 11: render_qc_text + write_qc_report_html
# ---------------------------------------------------------------------------

def test_render_qc_text(qc):
    s = qc.QCSummary("sbs",
                     [qc.Metric("single_map_rate", 0.55, "rate", status="pass",
                                threshold={"min": 0.45, "warn": 0.55})],
                     "pass", "2026-06-01T00:00:00Z")
    txt = qc.render_qc_text(s)
    assert "sbs" in txt
    assert "single_map_rate" in txt
    assert "PASS" in txt.upper()


def test_write_qc_report_html(qc, tmp_path):
    s = qc.QCSummary("sbs", [qc.Metric("single_map_rate", 0.55, "rate", status="pass")],
                     "pass", "2026-06-01T00:00:00Z")
    out = tmp_path / "qc_summary.html"
    qc.write_qc_report_html(s, out)
    html = out.read_text()
    assert "<html" in html.lower()
    assert "single_map_rate" in html


def test_build_phase_qc_unknown_phase_creates_eval_dir(qc, tmp_path):
    out = tmp_path / "brieflow_output"
    summary = qc.build_phase_qc(out, "cluster", generated_at="t")
    assert summary.metrics == []
    assert summary.overall_status == "info"
    written = out / "cluster" / "eval" / "qc_summary.json"
    assert written.exists()


def test_none_valued_metric_renders_na(qc, tmp_path):
    m = qc.Metric("x", None, status="info")
    s = qc.QCSummary("sbs", [m], "info", "2026-06-01T00:00:00Z")
    txt = qc.render_qc_text(s)
    assert "n/a" in txt
    out = tmp_path / "qc_na.html"
    qc.write_qc_report_html(s, out)
    html = out.read_text()
    assert "n/a" in html


# ---------------------------------------------------------------------------
# Change A: intra_cycle_channel_shift_residual_max_px is INFO-ONLY (never gated)
# ---------------------------------------------------------------------------

def test_parse_qc_block_intra_cycle_is_info_only(qc):
    """intra_cycle_channel_shift_residual_max_px must never gate qc_pass.

    A value of 553.0 (vastly above any reasonable threshold) paired with a
    passing cycle_dapi_shift_residual_max_px (6.0 < 10.0) must yield qc_pass=True.
    The 553.0 value must still appear in metrics (parsed but not gated).
    """
    import importlib.util as _ilu
    WIZARD = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_wizard.py"
    spec = _ilu.spec_from_file_location("brieflow_wizard_tst_a", WIZARD)
    wiz = _ilu.module_from_spec(spec)
    spec.loader.exec_module(wiz)

    stdout = (
        "intra_cycle_channel_shift_residual_max_px: 553.0\n"
        "cycle_dapi_shift_residual_max_px: 6.0\n"
    )
    result = wiz.parse_qc_block(stdout)
    # intra_cycle must NOT be in failed (info-only, never gated)
    assert "intra_cycle_channel_shift_residual_max_px" not in result["failed"]
    # cycle_dapi 6.0 < 10.0 → passes
    assert "cycle_dapi_shift_residual_max_px" not in result["failed"]
    assert result["qc_pass"] is True
    # The value 553.0 must still be present in the metrics dict
    assert result["metrics"]["intra_cycle_channel_shift_residual_max_px"] == pytest.approx(553.0)


def test_parse_qc_block_intra_cycle_info_only_no_gates(qc):
    """Even without gates/phase, intra_cycle does NOT fail qc_pass.

    Previously the default threshold was 1.0, so 553 would fail. After Change A
    the threshold is None → metric is parsed into metrics but never gated.
    """
    import importlib.util as _ilu
    WIZARD = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_wizard.py"
    spec = _ilu.spec_from_file_location("brieflow_wizard_tst_a2", WIZARD)
    wiz = _ilu.module_from_spec(spec)
    spec.loader.exec_module(wiz)

    stdout = "intra_cycle_channel_shift_residual_max_px: 553.0\n"
    result = wiz.parse_qc_block(stdout)
    assert "intra_cycle_channel_shift_residual_max_px" not in result["failed"]
    # Alone it is not evidence that QC ran, so the parse fails closed rather than passing.
    assert result["qc_missing"] is True and result["qc_pass"] is False
    assert result["metrics"]["intra_cycle_channel_shift_residual_max_px"] == pytest.approx(553.0)


# ---------------------------------------------------------------------------
# Change B: genes_per_cluster_at_target warn-band in build_phase_qc
# ---------------------------------------------------------------------------

def _make_cluster_fixture_local(base: Path, recall: float, cluster_sizes: list[int]) -> None:
    d = base / "CeCl-all_ChCo-DAPI" / "all" / "5"
    d.mkdir(parents=True)
    (d / "CB-Real__global_metrics.json").write_text(
        json.dumps({"STRING": {"recall": recall}})
    )
    n = len(cluster_sizes)
    clusters = {str(i): {"size": s} for i, s in enumerate(cluster_sizes)}
    (d / "CB-Real__integrated_results.json").write_text(
        json.dumps({"total_clusters_analyzed": n, "clusters": clusters})
    )


@pytest.mark.parametrize("gpk,expected_status", [
    (20.0, "pass"),   # exactly at target (20 ± 5) → pass
    (24.0, "pass"),   # 24 within [15, 25] → pass
    (15.0, "pass"),   # exactly at lower edge → pass
    (25.0, "pass"),   # exactly at upper edge → pass
    (12.2, "warn"),   # 12.2 < 15 → outside band → warn
    (26.0, "warn"),   # 26 > 25 → outside band → warn
])
def test_build_phase_qc_cluster_genes_per_cluster_warn_band(qc, tmp_path, gpk, expected_status):
    """genes_per_cluster_at_target: pass within [target-tol, target+tol], warn outside."""
    # gpk = total_genes / n_clusters; build sizes to hit gpk exactly
    out = tmp_path / f"brieflow_output_{gpk}"
    # Use 1 cluster with size=gpk*10, total_analyzed=10 to get exact mean
    cluster_dir = out / "cluster"
    d = cluster_dir / "CeCl-all_ChCo-DAPI" / "all" / "5"
    d.mkdir(parents=True)
    (d / "CB-Real__global_metrics.json").write_text(
        json.dumps({"STRING": {"recall": 0.15}})
    )
    total_genes = int(gpk * 10)
    (d / "CB-Real__integrated_results.json").write_text(
        json.dumps({
            "total_clusters_analyzed": 10,
            "clusters": {str(i): {"size": total_genes // 10} for i in range(10)},
        })
    )
    lab_defaults = {
        "cluster_selection": {
            "target_genes_per_cluster": 20,
            "genes_per_cluster_tolerance": 5,
        }
    }
    summary = qc.build_phase_qc(
        out, "cluster", generated_at="t",
        lab_defaults=lab_defaults,
    )
    by = {m.name: m for m in summary.metrics}
    assert "genes_per_cluster_at_target" in by
    assert by["genes_per_cluster_at_target"].status == expected_status, (
        f"gpk={gpk}: expected {expected_status}, got {by['genes_per_cluster_at_target'].status}"
    )


def test_build_phase_qc_cluster_genes_warn_never_fail(qc, tmp_path):
    """genes_per_cluster_at_target never has status=fail, only pass or warn."""
    out = tmp_path / "brieflow_output"
    cluster_dir = out / "cluster"
    d = cluster_dir / "CeCl-all_ChCo-DAPI" / "all" / "5"
    d.mkdir(parents=True)
    (d / "CB-Real__global_metrics.json").write_text(
        json.dumps({"STRING": {"recall": 0.15}})
    )
    # gpk = 1 (way outside any band)
    (d / "CB-Real__integrated_results.json").write_text(
        json.dumps({"total_clusters_analyzed": 100, "clusters": {str(i): {"size": 1} for i in range(100)}})
    )
    lab_defaults = {"cluster_selection": {"target_genes_per_cluster": 20, "genes_per_cluster_tolerance": 5}}
    summary = qc.build_phase_qc(out, "cluster", generated_at="t", lab_defaults=lab_defaults)
    by = {m.name: m for m in summary.metrics}
    assert by["genes_per_cluster_at_target"].status != "fail"
    assert by["genes_per_cluster_at_target"].status == "warn"


# ---------------------------------------------------------------------------
# Change C: extract_merge_qc no fallback — no merge_summary → []
# ---------------------------------------------------------------------------

def test_extract_merge_qc_no_summary_returns_empty(qc, tmp_path):
    """Old screens with no merge_summary.tsv return [] (no merge QC).

    The sbs_match_rate / ph_match_rate fallback has been removed.
    """
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    # Write only the old-style per-well matching-rate files (no merge_summary.tsv)
    sbs_hdr = "well\ttotal_cells\tmatched_cells\tmatch_rate"
    (eval_dir / "P-1_W-A1__final_sbs_matching_rates.tsv").write_text(
        f"{sbs_hdr}\nA1\t1000\t876\t87.6\n"
    )
    ph_hdr = "well\ttotal_cells\tmatched_cells\tmatch_rate"
    (eval_dir / "P-1_W-A1__final_phenotype_matching_rates.tsv").write_text(
        f"{ph_hdr}\nA1\t1000\t830\t83.0\n"
    )
    result = qc.extract_merge_qc(tmp_path)
    assert result == [], f"Expected [], got {result}"


# ---------------------------------------------------------------------------
# Pinned qc_gates.yaml values
# ---------------------------------------------------------------------------

def test_pinned_sbs_gates(qc):
    """Verify pinned threshold values for sbs phase_qc and sweep_qc."""
    gates = qc.load_qc_gates()
    pg = qc.phase_gates(gates, "sbs")
    # barcode-level gate, min lowered on operator call 2026-08-28
    assert pg["single_map_rate"] == {"min": 0.25, "warn": 0.40}
    assert pg["any_map_rate"] == {"min": 0.30, "warn": 0.50}
    assert pg["cell_retention"] == {"min": 0.80, "warn": 0.90}
    assert pg["nuclei_retention"] == {"min": 0.80, "warn": 0.90}
    sg = qc.sweep_gates(gates, "sbs")
    assert sg["cycle_dapi_shift_residual_max_px"] == {"max": 10.0}
    # intra_cycle is not in sweep_qc (INFO-ONLY — not a gate)
    assert "intra_cycle_channel_shift_residual_max_px" not in sg


def test_pinned_phenotype_gates(qc):
    """Verify pinned threshold values for phenotype phase_qc."""
    gates = qc.load_qc_gates()
    pg = qc.phase_gates(gates, "phenotype")
    assert pg["cell_retention"] == {"min": 0.75, "warn": 0.85}
    assert pg["nuclei_retention"] == {"min": 0.75, "warn": 0.85}


def test_pinned_merge_gates(qc):
    """Verify pinned threshold values for merge phase_qc."""
    gates = qc.load_qc_gates()
    pg = qc.phase_gates(gates, "merge")
    assert pg["ph_recovery_rate"] == {"min": 0.50, "warn": 0.65}
    assert "sbs_recovery_rate" not in pg
    assert pg["recovery_of_reachable"] == {"min": 0.50, "warn": 0.65}
    # min relaxed 0.30 -> 0.02 (2026-08-31): gene rate is set upstream by library
    # match, not by merge; only a collapsed barcode->gene join should block.
    assert pg["single_gene_rate"] == {"min": 0.02, "warn": 0.30}


def test_pinned_aggregate_gates(qc):
    """Verify pinned threshold value for aggregate phase_qc."""
    gates = qc.load_qc_gates()
    pg = qc.phase_gates(gates, "aggregate")
    assert pg["mean_percent_na"] == {"max": 0.40, "warn": 0.30}


def test_pinned_cluster_gates(qc):
    """Verify pinned threshold values for cluster phase_qc."""
    gates = qc.load_qc_gates()
    pg = qc.phase_gates(gates, "cluster")
    assert pg["best_recall"] == {"min": 0.10, "warn": 0.15}
    assert pg["recall_at_target_granularity"] == {"min": 0.08, "warn": 0.13}
    assert "genes_per_cluster_at_target" not in pg  # handled via warn-band, not normal gate


# ---------------------------------------------------------------------------
# Format-guard tests: missing columns must not raise, metric skipped
# ---------------------------------------------------------------------------

_OLD_SEG_HDR = "well\tnuclei"


def test_extract_sbs_qc_old_segmentation_format(qc, tmp_path):
    """Old etna-style segmentation with only 'nuclei' column: no error, retention metrics skipped.

    Mapping metrics are still returned when mapping files are present.
    """
    map_dir = tmp_path / "eval" / "mapping"
    seg_dir = tmp_path / "eval" / "segmentation"
    _write_map_file(map_dir / "P-1__mapping_overview.tsv", 1000, 500, 800)
    # Old format: only 'nuclei' column
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "P-1__segmentation_overview.tsv").write_text(
        f"{_OLD_SEG_HDR}\nA1\t1274150\nA2\t1264852\n"
    )

    metrics = qc.extract_sbs_qc(tmp_path)
    names = {m.name for m in metrics}

    # Must not raise; cell/nuclei_retention absent (columns missing)
    assert "cell_retention" not in names
    assert "nuclei_retention" not in names
    # Mapping metrics still present
    assert "single_map_rate" in names
    assert "total_cells" in names


def test_extract_sbs_qc_old_seg_no_mapping_no_error(qc, tmp_path):
    """Old-format segmentation only (no mapping) → returns [] without raising."""
    seg_dir = tmp_path / "eval" / "segmentation"
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "P-1__segmentation_overview.tsv").write_text(
        f"{_OLD_SEG_HDR}\nA1\t1274150\n"
    )
    metrics = qc.extract_sbs_qc(tmp_path)
    assert metrics == []


def test_extract_phenotype_qc_missing_columns(qc, tmp_path):
    """Segmentation file with only 'nuclei' column → no error, all metrics skipped."""
    seg_dir = tmp_path / "eval" / "segmentation"
    seg_dir.mkdir(parents=True, exist_ok=True)
    (seg_dir / "P-1__segmentation_overview.tsv").write_text(
        f"{_OLD_SEG_HDR}\nA1\t500000\n"
    )
    metrics = qc.extract_phenotype_qc(tmp_path)
    assert metrics == []


def test_extract_merge_qc_missing_columns(qc, tmp_path):
    """merge_summary.tsv missing expected rate columns → only present metrics emitted."""
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    # Write a merge_summary with only ph_recovery_rate/ph_cells (no sbs or single_gene)
    (eval_dir / "P-1__merge_summary.tsv").write_text(
        "well\tph_cells\tph_recovery_rate\nA1\t1000\t0.75\n"
    )
    metrics = qc.extract_merge_qc(tmp_path)
    names = {m.name for m in metrics}
    assert "ph_recovery_rate" in names
    assert "sbs_recovery_rate" not in names
    assert "single_gene_rate" not in names


def test_extract_aggregate_qc_missing_percent_na(qc, tmp_path):
    """na_stats.tsv missing 'percent_na' column → returns [] without raising."""
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "P-1__na_stats.tsv").write_text(
        "feature\tcount\nGENE_A\t100\n"
    )
    metrics = qc.extract_aggregate_qc(tmp_path)
    assert metrics == []


def test_extract_cluster_qc_malformed_json(qc, tmp_path):
    """Cluster JSON missing expected keys → returns metrics with None values, no error."""
    d = tmp_path / "CeCl-all_ChCo-DAPI" / "all" / "5"
    d.mkdir(parents=True)
    # Missing 'STRING' key entirely
    (d / "CB-Real__global_metrics.json").write_text(json.dumps({"OTHER": {}}))
    # No integrated_results.json
    metrics = qc.extract_cluster_qc(tmp_path)
    # Extractor has try/except; no valid recall found → returns []
    assert isinstance(metrics, list)


def _write_merge_eval(root, positions: bool):
    """A merge eval/ dir as either approach writes it: the shared eval_merge summary, plus
    the per-well positions QC tables and overlay images when positions image QC is on."""
    eval_dir = root / "merge" / "eval"
    eval_dir.mkdir(parents=True)
    (eval_dir / "P-1__merge_summary.tsv").write_text(
        "well\tph_cells\tph_recovery_rate\tsbs_cells\tsbs_recovery_rate\t"
        "total_match_pairs\tsingle_gene_rate\n"
        "A1\t1000\t0.80\t900\t0.70\t700\t0.40\n"
        "A2\t3000\t0.40\t2700\t0.45\t1500\t0.35\n"
    )
    if positions:
        (eval_dir / "P-1_W-A1__positions_merge_qc.tsv").write_text(
            "plate\twell\tstatus\tmatch_rate\tphenotype_overlap_agreement\t"
            "phenotype_orientation_warning\n1\tA1\tok\t0.01\t0.2\tTrue\n"
        )
        (eval_dir / "P-1_W-A1__positions_image_qc.tsv").write_text("kind\tresidual_px\n")
        for name in ("positions_tile_overlaps", "positions_phenotype_in_sbs", "positions_mosaic"):
            (eval_dir / f"P-1_W-A1__{name}.png").write_bytes(b"")


def test_positions_merge_gates_like_fast(qc, tmp_path):
    """The positions approach feeds the same eval_merge summary as fast, so merge QC reads
    the same metrics and reaches the same verdict; its per-well QC tables are not gated."""
    summaries = {}
    for approach in ("fast", "positions"):
        root = tmp_path / approach / "brieflow_output"
        _write_merge_eval(root, positions=approach == "positions")
        summaries[approach] = qc.build_phase_qc(root, "merge", "2026-01-01T00:00:00", config={})
    fast, positions = summaries["fast"], summaries["positions"]
    as_rows = lambda s: [(m.name, m.value, m.status) for m in s.metrics]  # noqa: E731
    assert as_rows(positions) == as_rows(fast)
    assert {m.name for m in positions.metrics} == {
        "ph_recovery_rate", "sbs_recovery_rate", "single_gene_rate"
    }
    assert [Path(f).name for f in positions.provenance] == ["P-1__merge_summary.tsv"]
    assert qc.qc_gate_verdict(positions) == qc.qc_gate_verdict(fast)
    # cell-weighted recovery 0.5 sits between min and warn, so both approaches warn alike
    assert positions.overall_status == fast.overall_status == "warn"


def test_parse_qc_block_fails_closed_without_a_qc_block(qc):
    """A notebook run that printed no QC block (verbose off) checked nothing: not a pass."""
    import importlib.util as _ilu
    WIZARD = Path(__file__).resolve().parent.parent / "scripts" / "brieflow_wizard.py"
    spec = _ilu.spec_from_file_location("brieflow_wizard_tst_missing", WIZARD)
    wiz = _ilu.module_from_spec(spec)
    spec.loader.exec_module(wiz)
    for stdout in ("", "intra_cycle_channel_shift_residual_max_px: 1.5\n"):
        result = wiz.parse_qc_block(stdout)
        assert result["qc_missing"] is True
        assert result["qc_pass"] is False
    inherited = wiz.parse_qc_block("", baseline={"metrics": {"cells_with_1_nucleus_frac": 0.97}})
    assert inherited["qc_missing"] is False and inherited["qc_pass"] is True
    seg = wiz.parse_qc_block("cells_with_1_nucleus_frac: 0.97\n")
    assert seg["qc_missing"] is False and seg["qc_pass"] is True
