"""Tests for the DET_RANGE probe (formerly a KNOWN_UNIMPLEMENTED probe)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture(scope="module")
def bw():
    spec = importlib.util.spec_from_file_location("brieflow_wizard", SCRIPTS / "brieflow_wizard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def probe(bw):
    # the facade's import populated sys.modules["brieflow_probe"]; probe_det_range
    # resolves _read_nd2_meta in THAT namespace, so monkeypatch there.
    return sys.modules["brieflow_probe"]


# --- DET_RANGE (ND2 voxel-size probe) ----------------------------------------
def test_det_range_from_pixel_sizes(probe, tmp_path, monkeypatch):
    sbs = tmp_path / "sbs"
    sbs.mkdir()
    ph = tmp_path / "ph"
    ph.mkdir()
    (sbs / "a.nd2").write_bytes(b"")
    (ph / "b.nd2").write_bytes(b"")
    # sbs 10x -> 0.65 µm/px ; phenotype 20x -> 0.325 µm/px ; M*B = 0.5
    meta = {str(sbs / "a.nd2"): {"voxel_um": (1.0, 0.65, 0.65)},
            str(ph / "b.nd2"): {"voxel_um": (1.0, 0.325, 0.325)}}
    monkeypatch.setattr(probe, "_read_nd2_meta", lambda p: meta.get(str(p), {}))
    screen = {"data": {"input_sbs_dir": str(sbs), "input_phenotype_dir": str(ph)}}
    out = probe.probe_det_range(tmp_path, screen)
    # DET_RANGE gates the affine determinant = (M*B)**2, so band = [0.9*mb**2, 1.1*mb**2]
    assert out["DET_RANGE"] == [round(0.9 * 0.5**2, 4), round(1.1 * 0.5**2, 4)]  # [0.225, 0.275]


def test_det_range_none_when_pixel_size_missing(probe, tmp_path, monkeypatch):
    sbs = tmp_path / "sbs"
    sbs.mkdir()
    (sbs / "a.nd2").write_bytes(b"")
    monkeypatch.setattr(probe, "_read_nd2_meta", lambda p: {"voxel_um": (1.0, 0.65, 0.65)})
    # no phenotype dir -> can't compute the ratio
    out = probe.probe_det_range(tmp_path, {"data": {"input_sbs_dir": str(sbs)}})
    assert out["DET_RANGE"] is None


def _phenix_index(dirpath: Path, resolution_m: str | None) -> None:
    """Minimal Opera Phenix Index.xml, shaped like Harmony's real output."""
    images = dirpath / "plate_1" / "Images"
    images.mkdir(parents=True)
    res = ""
    if resolution_m is not None:
        res = (f'<ImageResolutionX Unit="m">{resolution_m}</ImageResolutionX>'
               f'<ImageResolutionY Unit="m">{resolution_m}</ImageResolutionY>')
    (images / "Index.xml").write_text(
        f'<?xml version="1.0"?>\n<EvaluationInputData>\n<Images>\n<Image>\n'
        f'{res}<ImageSizeX>1080</ImageSizeX><BinningX>2</BinningX>\n'
        f'</Image>\n</Images>\n</EvaluationInputData>\n'
    )


# --- DET_RANGE (Opera Phenix Index.xml fallback) ------------------------------
def test_det_range_from_phenix_index_xml(probe, tmp_path):
    sbs = tmp_path / "sbs"
    ph = tmp_path / "ph"
    # real screen values: SBS 10x/bin2 = 1.1868 µm/px, phenotype 20x/bin2 = 0.5934 µm/px
    _phenix_index(sbs, "1.1867525298988041E-06")
    _phenix_index(ph, "5.93376264949402E-07")
    screen = {"data": {"input_sbs_dir": str(sbs), "input_phenotype_dir": str(ph)}}
    out = probe.probe_det_range(tmp_path, screen)
    assert out["DET_RANGE"] == [0.225, 0.275]


def test_det_range_none_when_index_xml_lacks_resolution(probe, tmp_path):
    sbs = tmp_path / "sbs"
    ph = tmp_path / "ph"
    _phenix_index(sbs, "1.1867525298988041E-06")
    _phenix_index(ph, None)
    screen = {"data": {"input_sbs_dir": str(sbs), "input_phenotype_dir": str(ph)}}
    assert probe.probe_det_range(tmp_path, screen)["DET_RANGE"] is None


def test_index_xml_pixel_size_none_without_index(probe, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert probe._index_xml_pixel_size_um(empty) is None


def test_det_range_registered_resolver(bw):
    assert bw.RESOLVER_MAP.get("DET_RANGE") == "probe_det_range"
    assert "DET_RANGE" not in bw.KNOWN_UNIMPLEMENTED_PROBES


def test_pixel_size_is_the_median_so_one_bad_file_cannot_set_it(monkeypatch, tmp_path):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "brieflow_probe", Path(__file__).resolve().parents[1] / "scripts" / "brieflow_probe.py")
    probe = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_probe"] = probe
    spec.loader.exec_module(probe)
    d = tmp_path / "sbs"
    (d / "empty_images").mkdir(parents=True)
    (d / "real_images").mkdir()
    (d / "empty_images" / "a.nd2").write_bytes(b"")
    for i in range(6):
        (d / "real_images" / f"t{i}.nd2").write_bytes(b"")
    monkeypatch.setattr(probe, "_read_nd2_meta", lambda p: {
        "voxel_um": (1.0, 65.0 if "empty" in str(p) else 0.65, 0.65)})
    assert probe._side_pixel_size_um(tmp_path, str(d), {}) == 0.65
