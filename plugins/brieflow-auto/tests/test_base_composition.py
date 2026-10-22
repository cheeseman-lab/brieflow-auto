"""Tests for brieflow_base_composition.py — called-base mix and all-on rate of a frac run."""
from __future__ import annotations

import pandas as pd
import pytest
import yaml

import brieflow_base_composition as bc
import brieflow_qc as qc

CODE = {"G": [], "T": ["Alexa 568"], "C": ["Alexa 488", "Alexa 568"], "A": ["Alexa 488", "Alexa 647"]}
DYES = ["Alexa 488", "Alexa 568", "Alexa 647"]


def _bases(states: list[list[tuple[int, int, int]]]) -> pd.DataFrame:
    """One row per read x cycle x dye; states[read][cycle] = (488, 568, 647) intensities."""
    rows = []
    for r, cycles in enumerate(states):
        for c, values in enumerate(cycles, start=1):
            for dye, v in zip(DYES, values):
                rows.append({"tile": 1, "read": r, "cycle": c, "channel": dye, "intensity": v})
    return pd.DataFrame(rows)


def _config(ambiguous_call=None) -> dict:
    comb = {"code": CODE}
    if ambiguous_call:
        comb["ambiguous_call"] = ambiguous_call
    return {"sbs": {
        "call_reads_method": "frac", "combinatorial": comb, "barcode_type": "multi",
        "map_start": 1, "map_end": 2, "recomb_start": 3, "recomb_end": 4,
        "prefix_map": "prefix_map", "prefix_recomb": "prefix_recomb",
        "df_barcode_library_fp": "config/barcode_library.tsv",
    }}


def _screen(tmp_path, barcodes: list[str], ambiguous_call=None, bases=None):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "config.yml").write_text(yaml.safe_dump(_config(ambiguous_call)))
    pd.DataFrame({"prefix_map": ["GT", "CA", "TC", "AG"], "prefix_recomb": ["GT", "CA", "TC", "AG"]}).to_csv(
        tmp_path / "config" / "barcode_library.tsv", sep="\t", index=False)
    well = tmp_path / "brieflow_output" / "sbs" / "parquets" / "1" / "r01" / "c01"
    well.mkdir(parents=True)
    pd.DataFrame({"barcode": barcodes}).to_parquet(well / "reads.parquet")
    if bases is not None:
        tile = tmp_path / "brieflow_output" / "sbs" / "tsvs" / "1" / "r01" / "c01" / "1"
        tile.mkdir(parents=True)
        bases.to_csv(tile / "bases.tsv", sep="\t", index=False)
    return tmp_path


def test_all_on_rate_counts_cycles_with_every_dye_on():
    # read 0: all-on, all-on, C, T; read 1: A, all-on, T, C  -> 3 of 8 bright cycles all-on
    df = _bases([
        [(100, 100, 100), (100, 100, 100), (150, 150, 0), (0, 300, 0)],
        [(150, 0, 150), (100, 100, 100), (0, 300, 0), (150, 150, 0)],
    ])
    rate, n = bc.all_on_rate(df, {"code": CODE})
    assert n == 8
    assert rate == pytest.approx(3 / 8)


def test_all_on_rate_ignores_blank_cycles():
    # the dim cycle is under half the read's median brightness -> blank, not counted
    df = _bases([[(100, 100, 100), (2, 2, 2), (150, 150, 0), (0, 300, 0)]])
    rate, n = bc.all_on_rate(df, {"code": CODE})
    assert n == 3
    assert rate == pytest.approx(1 / 3)


def test_all_on_rate_regions_index_cycle_rank_not_label():
    # cycles labelled 2..5 (cycle 1 skipped upstream). With one region over ranks 1-2 the
    # dim pair is referenced to its own median and stays bright.
    df = _bases([[(10, 10, 0), (10, 10, 0), (300, 300, 0), (300, 300, 0)]])
    df["cycle"] += 1
    _, n_global = bc.all_on_rate(df, {"code": CODE})
    _, n_region = bc.all_on_rate(df, {"code": CODE, "brightness_regions": [[1, 2], [3, 4]]})
    assert n_global == 2
    assert n_region == 4


def test_c_excess_and_unresolved_all_on_flagged(tmp_path):
    # every read C-rich against a library with one C per window; all-on cycles present
    bases = _bases([[(100, 100, 100), (150, 150, 0), (0, 300, 0), (150, 0, 150)]] * 3)
    screen = _screen(tmp_path, ["CCCC"] * 600 + ["GTCA"] * 600, bases=bases)
    rep = bc.base_composition(screen)
    assert rep["verdict"] == "measured"
    assert rep["base_excess_where"]["base"] == "C"
    assert rep["base_excess_max"] == pytest.approx(0.75)
    assert rep["all_on_rate"] == pytest.approx(0.25)
    assert rep["all_on_unresolved_rate"] == pytest.approx(0.25)
    assert rep["ambiguous_n_inert"] == 0.0


def test_ambiguous_n_configured_resolves_all_on(tmp_path):
    bases = _bases([[(100, 100, 100), (150, 150, 0), (0, 300, 0), (150, 0, 150)]] * 3)
    screen = _screen(tmp_path, ["NCAG"] * 600 + ["GTCA"] * 600, ambiguous_call="N", bases=bases)
    rep = bc.base_composition(screen)
    assert rep["all_on_unresolved_rate"] == 0.0
    assert rep["n_call_rate"] == pytest.approx(0.125)
    assert rep["ambiguous_n_inert"] == 0.0


def test_ambiguous_n_configured_but_no_n_in_reads_is_inert(tmp_path):
    screen = _screen(tmp_path, ["CCAG"] * 600 + ["GTCA"] * 600, ambiguous_call="N")
    rep = bc.base_composition(screen)
    assert rep["ambiguous_n_inert"] == 1.0
    assert rep["all_on_rate"] is None


def test_no_signal_reads_left_out(tmp_path):
    screen = _screen(tmp_path, ["GGGG"] * 5000 + ["GTCA"] * 1200)
    rep = bc.base_composition(screen)
    assert rep["n_reads"] == 1200
    assert rep["base_excess_max"] == pytest.approx(0.75)


def test_not_applicable_for_non_frac(tmp_path):
    screen = _screen(tmp_path, ["GTCA"] * 1200)
    cfg = _config()
    cfg["sbs"]["call_reads_method"] = "median"
    assert bc.base_composition(screen, cfg)["verdict"] == "not-applicable"


def test_insufficient_reads(tmp_path):
    screen = _screen(tmp_path, ["GTCA"] * 10)
    assert bc.base_composition(screen)["verdict"] == "insufficient"


def test_sbs_phase_qc_gates_unresolved_all_on(tmp_path):
    bases = _bases([[(100, 100, 100), (150, 150, 0), (0, 300, 0), (150, 0, 150)]] * 3)
    screen = _screen(tmp_path, ["CCCC"] * 600 + ["GTCA"] * 600, bases=bases)
    summary = qc.build_phase_qc(screen / "brieflow_output", "sbs", generated_at="t")
    by_name = {m.name: m for m in summary.metrics}
    assert by_name["all_on_unresolved_rate"].status == "fail"
    assert by_name["base_excess_max"].status == "fail"
    assert by_name["ambiguous_n_inert"].status == "pass"
    assert summary.overall_status == "fail"


def test_sbs_phase_qc_inert_n_fails(tmp_path):
    screen = _screen(tmp_path, ["CCAG"] * 600 + ["GTCA"] * 600, ambiguous_call="N")
    summary = qc.build_phase_qc(screen / "brieflow_output", "sbs", generated_at="t")
    by_name = {m.name: m for m in summary.metrics}
    assert by_name["ambiguous_n_inert"].status == "fail"
    assert by_name["all_on_unresolved_rate"].status == "info"


def test_sbs_phase_qc_without_config_adds_nothing(tmp_path):
    (tmp_path / "brieflow_output" / "sbs").mkdir(parents=True)
    summary = qc.build_phase_qc(tmp_path / "brieflow_output", "sbs", generated_at="t")
    assert not [m for m in summary.metrics if m.name in bc.METRICS]
