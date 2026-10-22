"""Base-order probe: the verdict logic (argmax of read-level library match over
channel<->base relabellings x cycle direction, vs the configured assignment), the
brightest-N threshold, library prefix regions, the preflight gate on the written
report, and the runner hook's marker parsing / leniency. The read path itself
(brieflow lib on a real tile) is exercised on a real screen tile, not here.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def probe():
    return _load("brieflow_base_order_probe")


@pytest.fixture(scope="module")
def preflight():
    return _load("brieflow_preflight")


def _res(bases, rev, match):
    return {"bases": list(bases), "reversed": rev, "match": match}


def _verdict(probe, results, bases=("T", "C"), chance=0.08, n=3000):
    return probe._verdict(results, list(bases), chance, n, 1, "r02c02", 1, [1, 2, 3], 500.0, (0, 7))


def test_verdict_ok_when_configured_is_argmax(probe):
    r = _verdict(probe, [_res("TC", False, 0.668), _res("TC", True, 0.414), _res("CT", False, 0.019), _res("CT", True, 0.012)])
    assert r["verdict"] == "ok" and r["suggested_bases"] is None and r["configured"]["match"] == 0.668
    assert r["results"][0]["match"] == 0.668  # sorted best-first


def test_verdict_mismatch_suggests_winning_order(probe):
    r = _verdict(probe, [_res("TC", False, 0.019), _res("TC", True, 0.012), _res("CT", False, 0.668), _res("CT", True, 0.414)])
    assert r["verdict"] == "mismatch" and r["suggested_bases"] == ["C", "T"]
    assert r["best"]["reversed"] is False


def test_verdict_mismatch_on_cycle_reversal(probe):
    r = _verdict(probe, [_res("TC", False, 0.10), _res("TC", True, 0.70), _res("CT", False, 0.02), _res("CT", True, 0.01)])
    assert r["verdict"] == "mismatch" and r["best"]["reversed"] is True


def test_verdict_ambiguous_near_chance_and_insufficient(probe):
    r = _verdict(probe, [_res("TC", False, 0.09), _res("CT", False, 0.11)], chance=0.08)
    assert r["verdict"] == "ambiguous"
    assert _verdict(probe, [], n=10)["verdict"] == "insufficient"


def test_verdict_tolerates_near_tie_with_configured(probe):
    # symmetric libraries can put a relabelling within noise of the configured one
    r = _verdict(probe, [_res("TC", False, 0.60), _res("CT", False, 0.62)])
    assert r["verdict"] == "ok"


def test_brightest_threshold(probe):
    peaks = np.zeros((10, 10))
    peaks.flat[:20] = np.arange(1, 21)
    assert probe._brightest_threshold(peaks, 0, 5) == 16.0   # 5th-brightest value; `>` admits at most 5
    assert probe._brightest_threshold(peaks, 0, 100) == 0.0  # fewer peaks than max → floor
    assert probe._brightest_threshold(peaks, 18, 5) == 18.0  # never below the configured floor


def test_library_prefixes_simple_and_multi(probe, tmp_path):
    (tmp_path / "config").mkdir()
    pd.DataFrame({"prefix": ["TTCTTCT", "CTCCGGC"]}).to_csv(tmp_path / "config" / "lib.tsv", sep="\t", index=False)
    prefixes, region = probe._library_prefixes(tmp_path, {"df_barcode_library_fp": "config/lib.tsv"})
    assert prefixes == {"TTCTTCT", "CTCCGGC"} and region == (0, 7)
    pd.DataFrame({"prefix_map": ["GATTACA", "CATCATG"], "prefix_recomb": ["GGGGG", "AAAAA"]}).to_csv(
        tmp_path / "config" / "multi.csv", index=False)
    prefixes, region = probe._library_prefixes(
        tmp_path, {"df_barcode_library_fp": "config/multi.csv", "barcode_type": "multi", "map_start": 0, "map_end": 7})
    assert prefixes == {"GATTACA", "CATCATG"} and region == (0, 7)


def test_region_length_mismatch_overrides_verdict_and_scans_dropped_cycle(probe):
    """Library prefixes one shorter than the MAP region (standardized with a skipped
    cycle the config does not declare): verdict becomes library-region-mismatch and
    the scan identifies which dropped cycle recovers the match."""
    prefixes = {"ACGTA", "TTGCA"}                      # 5 wide
    region = (0, 6)                                     # config says 6 wide
    df_bases = pd.DataFrame({"read": [0, 1]})
    calls = pd.DataFrame({"barcode": ["ACGCTA", "TTGACA"]})  # cycle 4 (index 3) is the extra one
    report = {"verdict": "ambiguous", "suggested_bases": ["x"], "diagnostics": {}}
    out = probe._check_region_length(report, prefixes, region, df_bases, ["a"], lambda *a, **k: calls,
                                     None, "frac", "combinatorial", None)
    assert out["verdict"] == "library-region-mismatch" and out["suggested_bases"] is None
    scan = out["diagnostics"]["dropped_cycle_scan"]
    assert scan["drop_cycle_4"] == 1.0 and max(v for k, v in scan.items() if k != "drop_cycle_4") < 1.0
    ok = probe._check_region_length({"verdict": "ok", "diagnostics": {}}, {"ACGTAA"}, region, None, [], None,
                                    None, "", "", None)
    assert ok["verdict"] == "ok"


def test_match_by_rank_orders_by_peak_score(probe):
    peaks = np.zeros((4, 4))
    peaks[0, 0], peaks[1, 1], peaks[2, 2] = 30, 20, 10  # brightest first
    df = pd.DataFrame({"i": [2, 0, 1], "j": [2, 0, 1], "barcode": ["XXXX", "ACGT", "ACGT"]})
    out = probe._match_by_rank(df, peaks, {"ACGT"}, (0, 4), ranks=(1, 2, 3))
    assert out == {"top_1": 1.0, "top_2": 1.0, "top_3": round(2 / 3, 4), "n": 3}


def test_map_window_scan_and_cycle_window_verdict(probe):
    """A staging mix-up: MAP barcode in staged cycles 6-12, not the configured 1-7."""
    peaks = np.ones((3, 3))
    df = pd.DataFrame({"i": [0, 1, 2], "j": [0, 1, 2], "barcode": ["GGGGGACGTAC", "GGGGGTTGCAA", "GGGGGACGTAC"]})
    prefixes = {"ACGTAC", "TTGCAA"}
    scan = probe._map_window_scan(df, peaks, prefixes, (0, 6))
    assert scan["cycles_6-11"] == 1.0 and scan["cycles_1-6"] == 0.0 and len(scan) == 6
    report = {"verdict": "ambiguous", "suggested_bases": ["x"], "diagnostics": {"configured_calls": {"map_window_scan": scan}}}
    out = probe._check_cycle_window(report, (0, 6), 0.01)
    assert out["verdict"] == "cycle-window-mismatch" and out["suggested_map_window"] == "cycles_6-11"
    ok = probe._check_cycle_window({"verdict": "ok", "diagnostics": {"configured_calls": {"map_window_scan": {"cycles_1-6": 0.7, "cycles_2-7": 0.1}}}}, (0, 6), 0.01)
    assert ok["verdict"] == "ok"


def test_sequencing_order_inferred_from_both_libraries(probe):
    """Recomb-first sequencing: RECOMB matches at cycles 1-5 and MAP at 6-12 while screen.yaml
    says map_recomb → sequencing-order-mismatch, and the cycle-window check must
    not then blame the cycle set."""
    calls = {"map_window_scan": {"cycles_1-7": 0.0, "cycles_6-12": 0.46},
             "recomb_window_scan": {"cycles_1-5": 0.52, "cycles_8-12": 0.07}}
    report = {"verdict": "ambiguous", "suggested_bases": ["x"], "diagnostics": {"configured_calls": calls}}
    out = probe._check_sequencing_order(report, (0, 7), 0.006)
    assert out["verdict"] == "sequencing-order-mismatch" and out["suggested_sequencing_order"] == "recomb_map"
    assert probe._check_cycle_window(out, (0, 7), 0.006)["verdict"] == "sequencing-order-mismatch"
    # consistent screen: MAP 1-7, RECOMB 8-12 under map_recomb → consistent, verdict untouched
    calls2 = {"map_window_scan": {"cycles_1-7": 0.25, "cycles_6-12": 0.01},
              "recomb_window_scan": {"cycles_1-5": 0.07, "cycles_8-12": 0.37}}
    ok = probe._check_sequencing_order({"verdict": "ok", "diagnostics": {"configured_calls": calls2}}, (0, 7), 0.006)
    assert ok["verdict"] == "ok" and ok["diagnostics"]["inferred_sequencing_order"] == "map_recomb"
    # no signal in either library → no inference
    weak = probe._check_sequencing_order({"verdict": "ambiguous", "diagnostics": {"configured_calls": {
        "map_window_scan": {"cycles_1-7": 0.01}, "recomb_window_scan": {"cycles_1-5": 0.02}}}}, (0, 7), 0.006)
    assert weak["verdict"] == "ambiguous" and "inferred_sequencing_order" not in weak["diagnostics"]


def test_preflight_sequencing_order_gate(preflight, tmp_path):
    _write_report(tmp_path, "sequencing-order-mismatch", suggested_sequencing_order="recomb_map",
                  diagnostics={"inferred_sequencing_order": "recomb_map", "configured_sequencing_order": "map_recomb"})
    (issue,) = preflight.validate_base_order_probe(tmp_path)
    assert issue.severity == "HIGH" and "sequencing_order: recomb_map" in issue.suggestion


def test_preflight_cycle_window_gate(preflight, tmp_path):
    _write_report(tmp_path, "cycle-window-mismatch", suggested_map_window="cycles_6-12", prefix_region=[0, 7],
                  diagnostics={"configured_calls": {"map_window_scan": {"cycles_1-7": 0.0, "cycles_6-12": 0.5}}})
    (issue,) = preflight.validate_base_order_probe(tmp_path)
    assert issue.severity == "HIGH" and "cycles_6-12" in issue.message and "configured cycles 1-7" in issue.message


def test_library_region_uses_brieflow_one_based_slice(probe, tmp_path):
    (tmp_path / "config").mkdir()
    pd.DataFrame({"prefix_map": ["GATTACA"]}).to_csv(tmp_path / "config" / "m.csv", index=False)
    _, region = probe._library_prefixes(
        tmp_path, {"df_barcode_library_fp": "config/m.csv", "barcode_type": "multi", "map_start": 1, "map_end": 7})
    assert region == (0, 7)  # prep_multi_reads: barcode[map_start-1 : map_end]


# --- preflight gate ------------------------------------------------------------
def _write_report(analysis_dir: Path, verdict: str, **extra):
    (analysis_dir / ".brieflow").mkdir(parents=True, exist_ok=True)
    rep = {"schema": "base-order-probe/v1", "verdict": verdict, "well": "r02c02", "tile": 1, "n_reads": 2999,
           "chance_match": 0.08, "configured": {"bases": ["T", "C"], "reversed": False, "match": 0.019},
           "best": {"bases": ["C", "T"], "reversed": False, "match": 0.668}, "suggested_bases": ["C", "T"]}
    rep.update(extra)
    (analysis_dir / ".brieflow" / "base_order_probe.json").write_text(json.dumps(rep))


def test_preflight_gate(preflight, tmp_path):
    assert preflight.validate_base_order_probe(tmp_path) == []  # no report → no-op
    _write_report(tmp_path, "ok")
    assert preflight.validate_base_order_probe(tmp_path) == []
    _write_report(tmp_path, "mismatch")
    (issue,) = preflight.validate_base_order_probe(tmp_path)
    assert issue.severity == "HIGH" and issue.code == "base-order-mismatch" and "['C', 'T']" in issue.suggestion
    _write_report(tmp_path, "ambiguous")
    (issue,) = preflight.validate_base_order_probe(tmp_path)
    assert issue.severity == "MEDIUM" and issue.code == "base-order-ambiguous"
    _write_report(tmp_path, "library-region-mismatch",
                  diagnostics={"prefix_length": 6, "region_length": 7, "dropped_cycle_scan": {"drop_cycle_6": 0.7}})
    (issue,) = preflight.validate_base_order_probe(tmp_path)
    assert issue.severity == "HIGH" and "6 wide" in issue.message and "drop_cycle_6" in issue.message


# --- runner hook -----------------------------------------------------------
def test_runner_hook_parses_markers_and_is_lenient(tmp_path, monkeypatch):
    ap = _load("brieflow_runner")
    nb = tmp_path / "2_sbs.py"
    nb.write_text("# === OPERATOR PARAMETERS ===\nTEST_PLATE = 1\nTEST_WELL = 'r02c02'  # phenix\nTEST_TILE = 1\n")
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd[-1])
        _write_report(tmp_path, "mismatch")
        return type("P", (), {"returncode": 2})()

    monkeypatch.setattr(ap.subprocess, "run", _fake_run)
    out = ap.run_base_order_probe(tmp_path, "env", nb)
    assert out["ran"] and out["verdict"] == "mismatch" and out["suggested_bases"] == ["C", "T"]
    assert "--plate 1 --well r02c02 --tile 1" in calls[0] and "conda activate env" in calls[0]
    # missing marker → skipped, never raises
    nb.write_text("TEST_PLATE = 1\n")
    assert ap.run_base_order_probe(tmp_path, "env", nb)["ran"] is False
