"""Preflight channel-name conventions the notebooks hardcode: phenotype needs a
channel literally named DAPI (a channel named "Hoechst" paused a run after SBS);
SBS needs DAPI at index 0 when the segmentation background comes from a
different cycle than DAPI (the composite-index bug)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location("brieflow_preflight", SCRIPTS / "brieflow_preflight.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_preflight"] = mod
    spec.loader.exec_module(mod)
    return mod


def _screen(tmp_path, ph_names, sbs_names, cyto_cycle=None, dapi_cycle=None):
    doc = {"phenotype": {"channels": [{"biological_name": n} for n in ph_names]},
           "iss": {"channels": [{"name": n} for n in sbs_names]}}
    if cyto_cycle is not None:
        doc["iss"]["background_channel_cell"] = {"channel": sbs_names[-1], "cycle": cyto_cycle}
    if dapi_cycle is not None:
        doc["iss"]["background_channel_nucleus"] = {"channel": "DAPI", "cycle": dapi_cycle}
    (tmp_path / "screen.yaml").write_text(yaml.safe_dump(doc))
    return tmp_path


def test_phenotype_nuclear_channel_must_be_named_dapi(tmp_path):
    pf = _load()
    (issue,) = pf.validate_channel_conventions(_screen(tmp_path, ["Phalloidin", "V5", "Hoechst"], ["DAPI", "T", "C"]))
    assert issue.severity == "HIGH" and issue.code == "phenotype-nuclear-channel-not-dapi"
    assert '"Hoechst" to biological_name "DAPI"' in issue.suggestion
    assert pf.validate_channel_conventions(_screen(tmp_path, ["Phalloidin", "V5", "DAPI"], ["DAPI", "T", "C"])) == []


def test_sbs_dapi_must_be_first_when_cycles_differ(tmp_path):
    pf = _load()
    (issue,) = pf.validate_channel_conventions(_screen(tmp_path, ["DAPI"], ["C", "T", "DAPI"], cyto_cycle=4))
    assert issue.code == "sbs-dapi-not-first" and "index 2" in issue.message
    assert pf.validate_channel_conventions(_screen(tmp_path, ["DAPI"], ["C", "T", "DAPI"], cyto_cycle=1)) == []
    assert pf.validate_channel_conventions(_screen(tmp_path, ["DAPI"], ["DAPI", "T", "C"], cyto_cycle=4)) == []


def test_no_screen_yaml_is_noop(tmp_path):
    assert _load().validate_channel_conventions(tmp_path) == []
