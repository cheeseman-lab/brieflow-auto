"""Named secondary objects: the goudacell segmentation_overrides hook, the per-object preflight
and the SECOND_OBJS marker round trip."""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import brieflow_interview as bi  # noqa: E402
import brieflow_markers as bm  # noqa: E402
import brieflow_preflight as pf  # noqa: E402

CHANNELS = ["DAPI", "COXIV", "G3BP1", "WGA"]
OVERRIDES = {
    "phenotype": {
        "cellpose_model": "/models/fine_tuned",
        "cell_flow_threshold": 0.6,
        "nuclei_cellprob_threshold": -1.0,
        "second_obj_detection": True,
        "second_obj_channel_index": 1,
        "second_objs": {
            "granule": {"second_obj_channel_index": 2, "threshold_method": "otsu_three_peak_mid_bg"}
        },
        "nuclei_diameter": 38.0,
        "use_ml_segmentation": False,
    }
}


def _interview(tmp_path, overrides=OVERRIDES, ref="goudacell_export.yaml"):
    screen = {
        "phenotype": {
            "channels": [{"biological_name": c} for c in CHANNELS],
            "segmentation_overrides": ref,
        }
    }
    (tmp_path / "screen.yaml").write_text(yaml.safe_dump(screen))
    if overrides is not None:
        (tmp_path / ref).write_text(yaml.safe_dump(overrides))
    iv_path = tmp_path / ".brieflow" / "interview.json"
    iv_path.parent.mkdir()
    bi.init_screen_interview(bi._DEFAULT_TEMPLATE, iv_path, "x")
    return bi.load_interview(iv_path), screen


def _row(iv, name):
    return bi.find_param(iv, name, notebook="phenotype")


def test_overrides_fill_matching_rows_with_provenance(tmp_path):
    iv, screen = _interview(tmp_path)
    bi.set_value(iv, "CELL_FLOW_THRESHOLD", 1.0, "operator", notebook="phenotype")
    bi.set_value(iv, "NUCLEI_CELLPROB_THRESHOLD", 0, "lab-default", notebook="phenotype")
    report = bi.apply_segmentation_overrides(iv, screen, tmp_path / "screen.yaml")

    assert report["error"] is None
    assert _row(iv, "CELLPOSE_MODEL")["value"] == "/models/fine_tuned"
    assert _row(iv, "CELLPOSE_MODEL")["value_source"] == "screen-yaml"
    assert _row(iv, "CELLPOSE_MODEL")["reason"] == "segmentation_overrides: goudacell_export.yaml"
    assert _row(iv, "NUCLEI_CELLPROB_THRESHOLD")["value"] == -1.0
    assert _row(iv, "SECOND_OBJ_DETECTION")["value"] is True
    assert _row(iv, "SECOND_OBJ_CHANNEL")["value"] == "COXIV"
    assert _row(iv, "SECOND_OBJS")["value"] == {
        "granule": {"channel": "G3BP1", "threshold_method": "otsu_three_peak_mid_bg"}
    }
    assert _row(iv, "CELL_FLOW_THRESHOLD")["value"] == 1.0
    assert report["kept"] == ["CELL_FLOW_THRESHOLD"]
    assert sorted(report["unmatched"]) == ["nuclei_diameter", "use_ml_segmentation"]


def test_overrides_report_unknown_channels_and_missing_files(tmp_path):
    bad = {"second_obj_channel_index": 9, "second_objs": {"granule": {"second_obj_channel_index": 7}}}
    iv, screen = _interview(tmp_path, bad)
    report = bi.apply_segmentation_overrides(iv, screen, tmp_path / "screen.yaml")
    assert sorted(report["unmatched"]) == ["second_obj_channel_index", "second_objs"]
    assert _row(iv, "SECOND_OBJS")["value"] is None

    missing = tmp_path / "missing"
    missing.mkdir()
    iv, screen = _interview(missing, overrides=None, ref="absent.yaml")
    report = bi.apply_segmentation_overrides(iv, screen, missing / "screen.yaml")
    assert report["error"] and not report["applied"]


def test_overrides_reapply_when_the_file_changes(tmp_path):
    iv, screen = _interview(tmp_path)
    bi.apply_segmentation_overrides(iv, screen, tmp_path / "screen.yaml")
    (tmp_path / "goudacell_export.yaml").write_text(yaml.safe_dump({"cellpose_model": "cyto3"}))
    bi.apply_segmentation_overrides(iv, screen, tmp_path / "screen.yaml")
    assert _row(iv, "CELLPOSE_MODEL")["value"] == "cyto3"


def _guards():
    return pf.BrieflowSurface(
        accepted_value_guards=[
            {
                "key": "threshold_method",
                "accepted": ["min_cross_entropy", "otsu_three_peak_mid_bg", "otsu_two_peak"],
                "file": "segment_secondary_object.py",
                "line": 1,
            }
        ]
    )


def test_preflight_checks_each_named_object():
    phenotype = {"channel_names": CHANNELS, "threshold_method": "otsu_two_peak"}
    good = {"phenotype": {**phenotype, "second_objs": {"granule": {"second_obj_channel_index": 2}}}}
    assert pf.validate_second_objs(good, _guards()) == []

    bad = {
        "phenotype": {
            **phenotype,
            "second_objs": {
                "granule": {"second_obj_channel_index": 2, "threshold_method": "otsu_3peak"},
                "cell_body": {"second_obj_channel_index": 9},
            },
        }
    }
    codes = sorted(i.code for i in pf.validate_second_objs(bad, _guards()))
    assert codes == ["CONFIG_VALUE_NOT_ACCEPTED", "SECOND_OBJ_CHANNEL_INVALID", "SECOND_OBJ_NAME_INVALID"]
    assert pf.validate_second_objs({"phenotype": phenotype}, _guards()) == []


def test_preflight_checks_count_mode_and_derived_objects():
    phenotype = {"channel_names": CHANNELS}
    good = {"phenotype": {**phenotype, "second_obj_channel_index": 2, "second_objs": {
        "foci": {"second_obj_mode": "count"},
        "dna": {"second_obj_channel_index": 0},
        "both": {"from": {"overlap": ["foci", "dna"]}},
        "rest": {"from": {"parent_minus": ["foci", "dna"]}},
    }}}
    assert pf.validate_second_objs(good, _guards()) == []

    bad = {"phenotype": {**phenotype, "second_obj_channel_index": 2, "second_objs": {
        "early": {"from": {"overlap": ["late"]}},
        "late": {"second_obj_mode": "tally"},
        "odd": {"from": {"union": ["late"]}},
    }}}
    codes = sorted(i.code for i in pf.validate_second_objs(bad, _guards()))
    assert codes == ["SECOND_OBJ_FROM_INVALID", "SECOND_OBJ_FROM_INVALID", "SECOND_OBJ_MODE_INVALID"]


def _canonical_phenotype() -> Path | None:
    d = os.environ.get("BRIEFLOW_CANONICAL_NB_DIR")
    p = Path(d) / "2_phenotype.py" if d else None
    return p if p and p.is_file() else None


@pytest.mark.skipif(_canonical_phenotype() is None, reason="BRIEFLOW_CANONICAL_NB_DIR not set")
def test_second_objs_dict_round_trips_through_the_marker(tmp_path):
    nb = tmp_path / "2_phenotype.py"
    nb.write_text(_canonical_phenotype().read_text())
    value = {"granule": {"channel": "G3BP1", "second_obj_max_size": 400, "max_total_objects": None}}
    assert bm.patch_marker_block_value(nb, "SECOND_OBJS", repr(value))
    line = next(x for x in nb.read_text().splitlines() if x.strip().startswith("SECOND_OBJS ="))
    assert ast.literal_eval(line.split("=", 1)[1].split("  #")[0].strip()) == value
