"""Regression tests for the code-review top-tier fixes (#1-#4):
#1 record_channel_mapping writes the new-model rows (CHANNEL_NAMES, uppercase).
#3 gated lab-defaults fill once their operator-answered driver is set (re-resolve).
#4 probe_preprocess fills path_metadata / metadata_order_type / channel_order_flip.
(#2 probe_det_range wiring is covered in test_resolve_phase.py.)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
DATA = SCRIPTS.parent / "data"
SCREENS = SCRIPTS.parent / "_test_screens"


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPTS / "brieflow_wizard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def probe(bw):
    return sys.modules["brieflow_probe"]


# --- #1 record_channel_mapping → CHANNEL_NAMES (uppercase, per-side) ----------
def _iv_with(rows):
    return {"params": rows}


def test_record_channel_mapping_writes_channel_names(bw):
    iv = _iv_with([
        {"notebook": "phenotype", "param_name": "CHANNEL_NAMES", "bucket": "user",
         "value": None, "value_source": None, "notes": ""},
    ])
    touched = bw.record_channel_mapping(iv, {"DAPI": "DAPI", "GFP": "COXIV"}, side="phenotype")
    cn = bw.find_param(iv, "CHANNEL_NAMES", notebook="phenotype")
    assert cn["value"] == ["DAPI", "COXIV"]
    assert cn["value_source"] == "operator"
    assert touched and touched[0] is cn


def test_record_channel_mapping_multiround_sets_round_order(bw):
    iv = _iv_with([
        {"notebook": "phenotype", "param_name": "CHANNEL_NAMES", "bucket": "user",
         "value": None, "value_source": None, "notes": ""},
        {"notebook": "preprocess", "param_name": "PHENOTYPE_ROUND_ORDER", "bucket": "auto-probe",
         "value": None, "value_source": None, "notes": ""},
    ])
    mapping = [{"DAPI": "DAPI", "GFP": "ATACsee"}, {"DAPI": "DAPI2", "GFP": "H3K4me3"}]
    bw.record_channel_mapping(iv, mapping, side="phenotype", round_order=[1, 2])
    assert bw.find_param(iv, "CHANNEL_NAMES", notebook="phenotype")["value"] == \
        ["DAPI", "ATACsee", "DAPI2", "H3K4me3"]
    assert bw.find_param(iv, "PHENOTYPE_ROUND_ORDER", notebook="preprocess")["value"] == [1, 2]


# --- #4 probe fills path_metadata / order_type / channel_order_flip -----------
def test_probe_fills_path_metadata_and_order_type(probe, tmp_path):
    sbs = tmp_path / "sbs"
    sbs.mkdir()
    # filename carries plate / well / tile / cycle + channel captures
    for w in ("A1", "A2"):
        (sbs / f"plate_1_Well{w}_Points-0001_c1_ChannelDAPI.tiff").write_bytes(b"")
    screen = {"data": {"input_sbs_dir": str(sbs)},
              "iss": {"channels": []}, "phenotype": {"channels": []}}
    rep = probe.probe_preprocess(tmp_path, screen)
    pf = rep["prefilled"]
    assert "plate" in pf["SBS_PATH_METADATA"] and "well" in pf["SBS_PATH_METADATA"]
    ot = pf["SBS_METADATA_ORDER_TYPE"]
    assert ot.get("well") == "str"
    assert ot.get("plate") == "int"
    assert pf["SBS_CHANNEL_ORDER_FLIP"] is False


# --- #3 gated lab-default fills after its operator driver is answered ---------
def test_gated_lab_default_fills_after_driver_answered(bw, tmp_path):
    ad = tmp_path / "d-analysis"
    ad.mkdir()
    (ad / "screen.yaml").write_text("screen_id: gating_test\npipeline: {}\n")
    tpl, lab = str(DATA / "interview_template.json"), str(DATA / "lab_defaults.yaml")
    bw.resolve_all_phases(ad, screen_yaml_path=ad / "screen.yaml", template_path=tpl, lab_defaults_path=lab)
    iv = bw.load_interview(ad / ".brieflow" / "interview.json")
    # The driver SEGMENTATION_METHOD is itself lab-filled (cellpose) in the same pass, and
    # the fixpoint then fills the cellpose-gated threshold.
    seg = bw.find_param(iv, "SEGMENTATION_METHOD", notebook="phenotype")
    assert seg["value"] == "cellpose" and seg["value_source"] == "lab-default"
    thr = bw.find_param(iv, "NUCLEI_FLOW_THRESHOLD", notebook="phenotype")
    assert thr["value"] == 0.4 and thr["value_source"] == "lab-default"
