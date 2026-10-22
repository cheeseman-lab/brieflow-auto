"""Two channel-name defects from a 4-color SBS / 5-channel phenotype screen: dye-named base
channels (BASES=[] in 2_sbs.py) and a phenotype channel with '_' that aggregate drops
(brieflow#314). Also: implausible pixel sizes no longer feed DET_RANGE."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_channel_conventions import _load, _screen  # noqa: E402

PH = ["DAPI", "BODIPY", "WGA", "MTR"]


def test_dye_named_base_channels_are_high(tmp_path):
    pf = _load()
    issues = pf.validate_channel_conventions(
        _screen(tmp_path, PH, ["DAPI", "Cy3", "A594", "Cy5", "Cy7"]))
    assert [i.code for i in issues] == ["sbs-channels-not-base-named"]
    assert issues[0].severity == "HIGH" and "BASES=[]" in issues[0].message


def test_base_named_and_two_color_pass(tmp_path):
    pf = _load()
    assert pf.validate_channel_conventions(_screen(tmp_path, PH, ["DAPI", "G", "T", "A", "C"])) == []
    assert pf.validate_channel_conventions(_screen(tmp_path, PH, ["DAPI", "T", "C"])) == []


def test_underscore_phenotype_channel_is_high(tmp_path):
    pf = _load()
    (issue,) = pf.validate_channel_conventions(
        _screen(tmp_path, ["DAPI", "BODIPY_Low", "BODIPY"], ["DAPI", "G", "T", "A", "C"]))
    assert issue.code == "phenotype-channel-name-underscore"
    assert issue.severity == "HIGH" and "BODIPY_Low" in issue.message and "#314" in issue.message


def test_implausible_pixel_size_leaves_det_range_to_operator(monkeypatch, tmp_path):
    import brieflow_probe as probe

    sizes = {"s": 17.0, "p": 0.607}
    monkeypatch.setattr(probe, "_side_pixel_size_um", lambda a, d, s: sizes[d])
    out = probe.probe_det_range(tmp_path, {"data": {"input_sbs_dir": "s", "input_phenotype_dir": "p"}})
    assert out["DET_RANGE"] is None and "implausible pixel size" in out["notes"][0]
    sizes["s"] = 1.214
    assert probe.probe_det_range(tmp_path, {"data": {"input_sbs_dir": "s",
                                                     "input_phenotype_dir": "p"}})["DET_RANGE"]


def test_underscore_channel_passes_once_brieflow_parses_combos(tmp_path):
    pf = _load()
    split = tmp_path / "bf" / "workflow" / "scripts" / "aggregate" / "split_datasets.py"
    split.parent.mkdir(parents=True)
    analysis = _screen(tmp_path, ["DAPI", "BODIPY_Low", "BODIPY"], ["DAPI", "G", "T", "A", "C"])
    split.write_text('channel_combo_list = channel_combo.split("_")\n')
    assert [i.code for i in pf.validate_channel_conventions(analysis, tmp_path / "bf")] == [
        "phenotype-channel-name-underscore"]
    split.write_text("channel_combo_list = parse_channel_combo(channel_combo, all_channels)\n")
    assert pf.validate_channel_conventions(analysis, tmp_path / "bf") == []


def test_cellpose_training_mode_argument_is_not_a_config_guard(tmp_path):
    pf = _load()
    lib = tmp_path / "workflow" / "lib"
    (lib / "shared").mkdir(parents=True)
    (lib / "shared" / "cellpose_training.py").write_text(
        'def f(mode):\n    if mode == "cells":\n        pass\n    else:\n'
        '        raise ValueError(f"Unknown mode: {mode}. Valid options: \'cells\', \'nuclei\'")\n')
    assert not [g for g in pf.analyze_brieflow(tmp_path).accepted_value_guards if g["key"] == "mode"]
