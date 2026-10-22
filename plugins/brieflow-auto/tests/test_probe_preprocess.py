"""Tests for probe_preprocess — the comprehensive preprocess data probe.

Inspects staged raw data and prefills the data-derivable preprocess interview
rows (format / pattern / organization / z / channel_order), validating ND2
channels against screen.yaml. ND2 reads go through the mockable `_read_nd2_meta`
seam so these tests need no binary fixtures. Honest limits: tiff channels +
missing/non-uniform data route to the operator; nothing is fabricated.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def probe():
    spec = importlib.util.spec_from_file_location("brieflow_probe", SCRIPTS / "brieflow_probe.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def _screen(sbs_dir=None, ph_dir=None, sbs_channels=None, ph_channels=None):
    return {
        "data": {"input_sbs_dir": sbs_dir, "input_phenotype_dir": ph_dir},
        "iss": {"channels": [{"name": n} for n in (sbs_channels or [])]},
        "phenotype": {"channels": [{"biological_name": n} for n in (ph_channels or [])]},
    }


# ---------------------------------------------------------------------------
# ND2: channels read from metadata, uniform -> channel_order=None, validated
# ---------------------------------------------------------------------------
def test_nd2_uniform_channels_prefills(probe, tmp_path, monkeypatch):
    sbs = tmp_path / "sbs"
    sbs.mkdir()
    (sbs / "WellA1_ChannelDAPI_Seq0000.nd2").write_bytes(b"")
    (sbs / "WellA2_ChannelDAPI_Seq0001.nd2").write_bytes(b"")

    monkeypatch.setattr(
        probe, "_read_nd2_meta",
        lambda p: {"channels": ["DAPI", "G", "T", "A", "C"], "n_z": None, "n_points": 4, "voxel_um": None},
    )
    monkeypatch.setattr(probe, "detect_organization", lambda *a, **k: "well")

    rep = probe.probe_preprocess(tmp_path, _screen(sbs_dir=str(sbs), sbs_channels=["DAPI", "G", "T", "A", "C"]))
    pf = rep["prefilled"]
    assert pf["SBS_DATA_FORMAT"] == "nd2"
    assert pf["SBS_DATA_ORGANIZATION"] == "well"
    assert pf["SBS_N_Z_PLANES"] is None        # z is internal to the container
    assert pf["SBS_CHANNEL_ORDER"] is None      # multichannel ND2 -> embedded
    assert "SBS_CHANNEL_ORDER" not in rep["operator_needed"]
    assert any("5 channels" in n for n in rep["notes"])


def test_nd2_channel_count_mismatch_warns(probe, tmp_path, monkeypatch):
    sbs = tmp_path / "sbs"
    sbs.mkdir()
    (sbs / "WellA1_x.nd2").write_bytes(b"")
    monkeypatch.setattr(probe, "_read_nd2_meta", lambda p: {"channels": ["DAPI", "G", "T", "A", "C"]})
    monkeypatch.setattr(probe, "detect_organization", lambda *a, **k: "well")
    # screen.yaml declares only 2 channels -> reconcile warning
    rep = probe.probe_preprocess(tmp_path, _screen(sbs_dir=str(sbs), sbs_channels=["DAPI", "G"]))
    assert any("reconcile" in n for n in rep["notes"])


def test_nd2_non_uniform_channels_routes_operator(probe, tmp_path, monkeypatch):
    sbs = tmp_path / "sbs"
    sbs.mkdir()
    (sbs / "a.nd2").write_bytes(b"")
    (sbs / "b.nd2").write_bytes(b"")
    seq = iter([{"channels": ["DAPI", "G"]}, {"channels": ["DAPI", "G", "T"]}])
    monkeypatch.setattr(probe, "_read_nd2_meta", lambda p: next(seq))
    monkeypatch.setattr(probe, "detect_organization", lambda *a, **k: "well")
    rep = probe.probe_preprocess(tmp_path, _screen(sbs_dir=str(sbs), sbs_channels=["DAPI", "G"]))
    assert "SBS_CHANNEL_ORDER" in rep["operator_needed"]
    assert any("NOT uniform" in n for n in rep["notes"])


# ---------------------------------------------------------------------------
# TIFF: per-tile; channel_order extracted from the filename Channel token
# (tiff metadata has no channels, but the filename does).
# ---------------------------------------------------------------------------
def test_tiff_channel_order_from_filename_tokens(probe, tmp_path):
    ph = tmp_path / "ph"
    ph.mkdir()
    (ph / "WellA1_ChannelDAPI.tiff").write_bytes(b"")
    (ph / "WellA1_ChannelGFP.tiff").write_bytes(b"")
    rep = probe.probe_preprocess(tmp_path, _screen(ph_dir=str(ph), ph_channels=["DAPI", "GFP"]))
    pf = rep["prefilled"]
    assert pf["PHENOTYPE_DATA_FORMAT"] == "tiff"
    assert pf["PHENOTYPE_DATA_ORGANIZATION"] == "tile"   # forced — tiff is per-tile
    assert pf["PHENOTYPE_CHANNEL_ORDER"] == ["DAPI", "GFP"]   # from filename Channel tokens
    assert "PHENOTYPE_CHANNEL_ORDER" not in rep["operator_needed"]


def test_tiff_no_channel_token_routes_operator(probe, tmp_path):
    ph = tmp_path / "ph"
    ph.mkdir()
    # No `Channel<x>` token in the filename -> channel order is genuinely unknown.
    (ph / "WellA1_Tile1.tiff").write_bytes(b"")
    (ph / "WellA2_Tile1.tiff").write_bytes(b"")
    rep = probe.probe_preprocess(tmp_path, _screen(ph_dir=str(ph)))
    assert rep["prefilled"]["PHENOTYPE_DATA_FORMAT"] == "tiff"
    assert "PHENOTYPE_CHANNEL_ORDER" in rep["operator_needed"]
    assert any("no Channel token" in n for n in rep["notes"])


# ---------------------------------------------------------------------------
# round_order: scanned from round_N dirs on the phenotype side (wired probe).
# ---------------------------------------------------------------------------
def test_phenotype_round_order_detected_from_dirs(probe, tmp_path):
    ph = tmp_path / "ph"
    (ph / "round_1").mkdir(parents=True)
    (ph / "round_2").mkdir()
    (ph / "round_1" / "WellA1_ChannelDAPI.tiff").write_bytes(b"")
    (ph / "round_2" / "WellA1_ChannelDAPI.tiff").write_bytes(b"")
    rep = probe.probe_preprocess(tmp_path, _screen(ph_dir=str(ph)))
    assert rep["prefilled"]["PHENOTYPE_ROUND_ORDER"] == [1, 2]
    assert "PHENOTYPE_ROUND_ORDER" not in rep["operator_needed"]


def test_phenotype_round_order_defaults_single(probe, tmp_path):
    ph = tmp_path / "ph"
    ph.mkdir()
    (ph / "WellA1_ChannelDAPI.tiff").write_bytes(b"")
    rep = probe.probe_preprocess(tmp_path, _screen(ph_dir=str(ph)))
    assert rep["prefilled"]["PHENOTYPE_ROUND_ORDER"] is None   # one round: no round filter


# ---------------------------------------------------------------------------
# Missing / unstaged data -> operator, no fabrication
# ---------------------------------------------------------------------------
def test_missing_dir_routes_operator(probe, tmp_path):
    rep = probe.probe_preprocess(tmp_path, _screen(sbs_dir=str(tmp_path / "nope")))
    assert "SBS_CHANNEL_ORDER" in rep["operator_needed"]
    assert any("not found" in n for n in rep["notes"])


def test_no_dir_in_screen_yaml_noted(probe, tmp_path):
    rep = probe.probe_preprocess(tmp_path, _screen())  # both dirs None
    assert rep["prefilled"] == {}
    assert any("no input dir" in n for n in rep["notes"])


def test_relative_dir_resolved_against_analysis_dir(probe, tmp_path, monkeypatch):
    (tmp_path / "raw_sbs").mkdir()
    (tmp_path / "raw_sbs" / "x.nd2").write_bytes(b"")
    monkeypatch.setattr(probe, "_read_nd2_meta", lambda p: {"channels": ["DAPI"]})
    monkeypatch.setattr(probe, "detect_organization", lambda *a, **k: "well")
    rep = probe.probe_preprocess(tmp_path, _screen(sbs_dir="raw_sbs", sbs_channels=["DAPI"]))
    assert rep["prefilled"]["SBS_DATA_FORMAT"] == "nd2"  # relative path resolved


# ---------------------------------------------------------------------------
# _tokenize_filename — splits an Opera Phenix / generic split-tiff basename
# into (prefix, value) runs for downstream role inference.
# ---------------------------------------------------------------------------
def test_tokenize_phenix_filename(probe):
    toks = probe._tokenize_filename("r02c02f01p01-ch1sk1fk1fl1.tiff")
    assert [(t["prefix"], t["value"]) for t in toks] == [
        ("r", "02"), ("c", "02"), ("f", "01"), ("p", "01"),
        ("ch", "1"), ("sk", "1"), ("fk", "1"), ("fl", "1"),
    ]


# ---------------------------------------------------------------------------
# _classify_filename_tokens — classifies tokenized prefixes into well/tile/
# channel/z roles by cardinality + prefix hints.
# ---------------------------------------------------------------------------
def test_classify_phenix_sbs_tokens(probe):
    names = [f"r0{r}c0{c}f{t:02d}p0{z}-ch{ch}sk1fk1fl1.tiff"
             for r in (2,3) for c in (2,3) for t in range(1,6)
             for z in (1,2,3) for ch in (1,2)]
    r = probe._classify_filename_tokens(names, screen_channel_count=2)
    assert r["well_prefixes"] == ["r", "c"]
    assert r["tile_prefix"] == "f"
    assert r["channel_prefix"] == "ch"   # prefix-hint 'ch' (and count==2)
    assert r["z_prefix"] == "p"
    assert r["ambiguous"] == []


def test_classify_ambiguous_channel_z_routes(probe):
    # two low-card dims 'a' and 'b', no hint, equal counts -> ambiguous
    names = [f"w{w}f{t}a{a}b{b}.tiff" for w in (1,2) for t in range(1,6)
             for a in (1,2,3) for b in (1,2,3)]
    r = probe._classify_filename_tokens(names, screen_channel_count=None)
    assert set(r["ambiguous"]) == {"a", "b"}


def test_classify_real_multiwell_hint_primary(probe):
    names = [f"r0{r}c0{c}f{t:02d}p0{z}-ch{ch}sk1fk1fl1.tiff"
             for r in range(2, 7) for c in range(2, 6) for t in range(1, 4)
             for z in (1, 2, 3) for ch in (1, 2)]
    r = probe._classify_filename_tokens(names, screen_channel_count=2)
    assert r["well_prefixes"] == ["r", "c"]
    assert r["tile_prefix"] == "f"
    assert r["channel_prefix"] == "ch"
    assert r["z_prefix"] == "p"
    assert r["ambiguous"] == []


# ---------------------------------------------------------------------------
# infer_split_tiff_layout — regex + path_metadata + channel_order + n_z_planes
# for a generic (Opera Phenix-style) split-tiff filename convention.
# ---------------------------------------------------------------------------
def test_infer_split_tiff_phenix_sbs(probe):
    rel = [f"plate_1/c{cy}/Images/r02c0{c}f{t:02d}p0{z}-ch{ch}sk1fk1fl1.tiff"
           for cy in (1,2) for c in (2,3) for t in (1,2) for z in (1,2,3)
           for ch in ((1,2,3) if cy == 1 else (1,2))]
    out = probe.infer_split_tiff_layout(rel, side="sbs", screen_channel_count=2)
    assert out["path_metadata"] == ["plate", "cycle", "well", "tile", "channel", "z"]
    assert out["metadata_order_type"]["well"] == "str"
    assert out["metadata_order_type"]["channel"] == "str"
    assert out["n_z_planes"] == 3
    assert out["channel_order"] == ["ch1", "ch2", "ch3"]  # natural sort, union across cycles
    import re
    m = re.compile(out["path_pattern"]).search(rel[0])
    assert m and m.group(3) == "r02c02"          # well compound


# ---------------------------------------------------------------------------
# detect_z_planes — distinct-z-value mode (z_group_index), vs the existing
# files-per-tuple mode (z_group_index=None).
# ---------------------------------------------------------------------------
def test_detect_z_planes_distinct_mode(probe, tmp_path):
    d = tmp_path / "sbs"
    for t in (1, 2):
        for z in (1, 2, 3):
            for ch in (1, 2):
                (d / "plate_1" / "c1" / "Images").mkdir(parents=True, exist_ok=True)
                (d / "plate_1" / "c1" / "Images" / f"r02c02f0{t}p0{z}-ch{ch}sk1fk1fl1.tiff").write_bytes(b"")
    pat = r"plate_(\d+).*c(\d+)/.*(r\d+c\d+)f(\d+)p(\d+)-(ch\d+)"
    # groups: 1 plate,2 cycle,3 well,4 tile,5 z,6 channel  -> z_group_index=4 (0-based)
    assert probe.detect_z_planes(d, pat, "tiff", z_group_index=4) == 3


# ---------------------------------------------------------------------------
# probe_preprocess (tiff) — split-tiff layout tried first, named-token
# infer_path_pattern as fallback when it returns None.
# ---------------------------------------------------------------------------
def test_probe_preprocess_phenix_sbs_uses_split_tiff_layout(probe, tmp_path):
    sbs = tmp_path / "sbs"
    for cy in (1, 2):
        d = sbs / "plate_1" / f"c{cy}" / "Images"
        d.mkdir(parents=True)
        chans = (1, 2, 3) if cy == 1 else (1, 2)
        for c in (2, 3):
            for t in (1, 2):
                for z in (1, 2, 3):
                    for ch in chans:
                        (d / f"r02c0{c}f{t:02d}p0{z}-ch{ch}sk1fk1fl1.tiff").write_bytes(b"")
    screen = {
        "data": {"input_sbs_dir": str(sbs)},
        "iss": {"channels": [{"name": n} for n in ("DAPI", "G", "T")]},
        "phenotype": {"channels": []},
    }
    rep = probe.probe_preprocess(tmp_path, screen)
    pf = rep["prefilled"]
    assert pf["SBS_DATA_FORMAT"] == "tiff"
    assert pf["SBS_PATH_METADATA"] == ["plate", "cycle", "well", "tile", "channel", "z"]
    assert pf["SBS_METADATA_ORDER_TYPE"]["well"] == "str"
    assert pf["SBS_N_Z_PLANES"] == 3
    assert pf["SBS_CHANNEL_ORDER"] == ["ch1", "ch2", "ch3"]   # union across cycles
    assert "SBS_CHANNEL_ORDER" not in rep["operator_needed"]
    assert "SBS_PATH_PATTERN" not in rep["operator_needed"]
    import re
    m = re.compile(pf["SBS_PATH_PATTERN"]).search(
        str(next((sbs / "plate_1" / "c1" / "Images").glob("*.tiff")).relative_to(sbs))
    )
    assert m and m.group(3).startswith("r02c0")


def test_probe_preprocess_index_xml_emits_external_metadata(probe, tmp_path):
    sbs = tmp_path / "sbs"
    for cy in (1, 2):
        d = sbs / "plate_1" / f"c{cy}" / "Images"
        d.mkdir(parents=True)
        for t in (1, 2):
            for ch in (1, 2):
                (d / f"r02c02f{t:02d}p01-ch{ch}sk1fk1fl1.tiff").write_bytes(b"")
        (d / "Index.xml").write_text("<Images></Images>")
    screen = {
        "data": {"input_sbs_dir": str(sbs)},
        "iss": {"channels": [{"name": n} for n in ("C", "T")]},
        "phenotype": {"channels": []},
    }
    rep = probe.probe_preprocess(tmp_path, screen)
    pf = rep["prefilled"]
    assert pf["SBS_METADATA_IMAGES_DIR_FP"] == str(sbs)
    assert pf["SBS_METADATA_PATH_METADATA"] == ["plate", "cycle"]
    assert pf["SBS_METADATA_PATH_ORDER_TYPE"] == {"plate": "int", "cycle": "int"}
    import re

    m = re.compile(pf["SBS_METADATA_PATH_PATTERN"]).search(
        str((sbs / "plate_1" / "c2" / "Images" / "Index.xml"))
    )
    assert m and (int(m.group(1)), int(m.group(2))) == (1, 2)


def test_probe_preprocess_no_index_xml_no_external_metadata(probe, tmp_path):
    sbs = tmp_path / "sbs"
    d = sbs / "plate_1" / "c1" / "Images"
    d.mkdir(parents=True)
    for t in (1, 2):
        for ch in (1, 2):
            (d / f"r02c02f{t:02d}p01-ch{ch}sk1fk1fl1.tiff").write_bytes(b"")
    screen = {"data": {"input_sbs_dir": str(sbs)}, "iss": {"channels": []}, "phenotype": {"channels": []}}
    pf = probe.probe_preprocess(tmp_path, screen)["prefilled"]
    assert "SBS_METADATA_IMAGES_DIR_FP" not in pf
    assert "SBS_METADATA_PATH_PATTERN" not in pf


def test_probe_preprocess_phenix_phenotype_uses_split_tiff_layout(probe, tmp_path):
    ph = tmp_path / "ph"
    d = ph / "Images"
    d.mkdir(parents=True)
    for c in (2, 3):
        for t in (1, 2):
            for z in (1, 2):
                for ch in (1, 2, 3, 4):
                    (d / f"r02c0{c}f{t:02d}p0{z}-ch{ch}sk1fk1fl1.tiff").write_bytes(b"")
    screen = {
        "data": {"input_phenotype_dir": str(ph)},
        "iss": {"channels": []},
        "phenotype": {
            "channels": [{"biological_name": n} for n in ("DAPI", "COX4", "WGA", "Tubulin")]
        },
    }
    rep = probe.probe_preprocess(tmp_path, screen)
    pf = rep["prefilled"]
    assert pf["PHENOTYPE_DATA_FORMAT"] == "tiff"
    # no plate_N / round_N dirs present in this fixture -> those groups are omitted
    assert pf["PHENOTYPE_PATH_METADATA"] == ["well", "tile", "channel", "z"]
    assert pf["PHENOTYPE_N_Z_PLANES"] == 2
    assert pf["PHENOTYPE_CHANNEL_ORDER"] == ["ch1", "ch2", "ch3", "ch4"]
    assert "PHENOTYPE_CHANNEL_ORDER" not in rep["operator_needed"]
    assert "PHENOTYPE_PATH_PATTERN" not in rep["operator_needed"]
    assert pf["PHENOTYPE_ROUND_ORDER"] is None   # no round_N dirs -> one round, no filter


def test_probe_preprocess_nikon_tiff_falls_back_when_split_tiff_layout_fails(probe, tmp_path):
    # Named-token Nikon-style filenames have no coherent well+tile *token*
    # structure for _classify_filename_tokens -> infer_split_tiff_layout
    # must return None so this hits the EXISTING infer_path_pattern path,
    # unchanged (regression guard for the wiring itself, not just the
    # standalone functions).
    ph = tmp_path / "ph"
    ph.mkdir()
    (ph / "WellA1_ChannelDAPI.tiff").write_bytes(b"")
    (ph / "WellA1_ChannelGFP.tiff").write_bytes(b"")
    rep = probe.probe_preprocess(tmp_path, _screen(ph_dir=str(ph), ph_channels=["DAPI", "GFP"]))
    pf = rep["prefilled"]
    assert pf["PHENOTYPE_CHANNEL_ORDER"] == ["DAPI", "GFP"]   # named Channel token, not split-tiff
    assert "PHENOTYPE_CHANNEL_ORDER" not in rep["operator_needed"]


# ---------------------------------------------------------------------------
# apply_preprocess_probe — write report.prefilled into the interview so the
# operator isn't asked for what the probe resolved.
# ---------------------------------------------------------------------------
TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "interview_template.json"


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPTS / "brieflow_wizard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_apply_preprocess_probe_fills_with_auto_probe_source(bw, tmp_path):
    dst = tmp_path / ".brieflow" / "interview.json"
    bw.init_screen_interview(TEMPLATE, dst, "t")
    iv = bw.load_interview(dst)
    report = {
        "prefilled": {
            "SBS_DATA_FORMAT": "nd2",
            "SBS_DATA_ORGANIZATION": "well",
            "SBS_CHANNEL_ORDER": None,   # None is a real answer for multichannel ND2
        },
        "operator_needed": ["PHENOTYPE_CHANNEL_ORDER"],
        "notes": [],
    }
    written = bw.apply_preprocess_probe(iv, report)
    assert set(written) == {"SBS_DATA_FORMAT", "SBS_DATA_ORGANIZATION", "SBS_CHANNEL_ORDER"}

    org = bw.find_param(iv, "SBS_DATA_ORGANIZATION", notebook="preprocess")
    assert org["value"] == "well" and org["value_source"] == "auto-probe"
    # channel_order None but FILLED (value_source set) -> resolve_all_phases skips it
    co = bw.find_param(iv, "SBS_CHANNEL_ORDER", notebook="preprocess")
    assert co["value"] is None and co["value_source"] == "auto-probe"
    # the operator_needed row was NOT touched -> stays a gap to ask
    ph = bw.find_param(iv, "PHENOTYPE_CHANNEL_ORDER", notebook="preprocess")
    assert ph["value_source"] is None


def test_apply_preprocess_probe_fills_phenix_split_tiff_end_to_end(bw, probe, tmp_path):
    # End-to-end: real Opera Phenix split-tiff fixture -> probe_preprocess ->
    # apply_preprocess_probe -> interview rows carry value_source=auto-probe.
    sbs = tmp_path / "sbs"
    d = sbs / "plate_1" / "c1" / "Images"
    d.mkdir(parents=True)
    for c in (2, 3):
        for t in (1, 2):
            for z in (1, 2, 3):
                for ch in (1, 2):
                    (d / f"r02c0{c}f{t:02d}p0{z}-ch{ch}sk1fk1fl1.tiff").write_bytes(b"")
    screen = {
        "data": {"input_sbs_dir": str(sbs)},
        "iss": {"channels": [{"name": n} for n in ("DAPI", "G")]},
        "phenotype": {"channels": []},
    }
    report = probe.probe_preprocess(tmp_path, screen)

    dst = tmp_path / ".brieflow" / "interview.json"
    bw.init_screen_interview(TEMPLATE, dst, "t")
    iv = bw.load_interview(dst)
    written = bw.apply_preprocess_probe(iv, report)
    assert {
        "SBS_DATA_FORMAT", "SBS_PATH_PATTERN", "SBS_PATH_METADATA", "SBS_METADATA_ORDER_TYPE",
        "SBS_DATA_ORGANIZATION", "SBS_N_Z_PLANES", "SBS_CHANNEL_ORDER", "SBS_CHANNEL_ORDER_FLIP",
    } <= set(written)

    pp = bw.find_param(iv, "SBS_PATH_METADATA", notebook="preprocess")
    assert pp["value"] == ["plate", "cycle", "well", "tile", "channel", "z"]
    assert pp["value_source"] == "auto-probe"

    ot = bw.find_param(iv, "SBS_METADATA_ORDER_TYPE", notebook="preprocess")
    assert ot["value"]["well"] == "str" and ot["value_source"] == "auto-probe"

    nz = bw.find_param(iv, "SBS_N_Z_PLANES", notebook="preprocess")
    assert nz["value"] == 3 and nz["value_source"] == "auto-probe"

    co = bw.find_param(iv, "SBS_CHANNEL_ORDER", notebook="preprocess")
    assert co["value"] == ["ch1", "ch2"] and co["value_source"] == "auto-probe"

    assert "SBS_CHANNEL_ORDER" not in report["operator_needed"]
    assert "SBS_PATH_PATTERN" not in report["operator_needed"]


def test_ims_prefills_tile_organization(probe, tmp_path):
    sbs = tmp_path / "sbs"
    sbs.mkdir()
    (sbs / "plate1_A1_F0001.ims").write_bytes(b"")
    (sbs / "plate1_A1_F0002.ims").write_bytes(b"")

    assert probe.detect_data_format(sbs) == "ims"
    assert probe.detect_organization(sbs) == "tile"

    rep = probe.probe_preprocess(tmp_path, _screen(sbs_dir=str(sbs), sbs_channels=["DAPI", "G"]))
    pf = rep["prefilled"]
    assert pf["SBS_DATA_FORMAT"] == "ims"
    assert pf["SBS_DATA_ORGANIZATION"] == "tile"
    assert pf["SBS_N_Z_PLANES"] is None        # z is max-projected by the reader


def test_sbs_cycles_not_in_dirs_stops_probe(probe, tmp_path):
    sbs = tmp_path / "sbs"
    sbs.mkdir()
    for c in (1, 2):
        (sbs / f"P001_SBS_10x_C{c}_Wells-A1_Points-000.nd2").write_bytes(b"")
    screen = _screen(sbs_dir=str(sbs))
    screen["iss"]["cycles"] = 2
    rep = probe.probe_preprocess(tmp_path, screen)
    assert "SBS_PATH_PATTERN" not in rep["prefilled"]
    assert "SBS_PATH_PATTERN" in rep["operator_needed"]
    assert any("not separated into one c<M>/ dir" in n for n in rep["notes"])


def test_has_cycle_dirs(probe, tmp_path):
    (tmp_path / "plate_1" / "c1").mkdir(parents=True)
    assert probe.has_cycle_dirs(tmp_path, 12)
    flat = tmp_path / "flat"
    flat.mkdir()
    assert not probe.has_cycle_dirs(flat, 12)
    assert probe.has_cycle_dirs(flat, 1)
    assert probe.has_cycle_dirs(flat, None)
