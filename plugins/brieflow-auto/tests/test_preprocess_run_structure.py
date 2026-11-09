"""brieflow's preprocess run structure: grouped Slurm jobs, folded metadata rules, its own
memory sizing, and SBS cycles stacked on C (brieflow #344, #346)."""

from __future__ import annotations

import json
import sys
import types

import numpy as np
import pytest

import brieflow_dryrun_preview as preview
import brieflow_resolve_resources as resolver
from brieflow_aggregate_efficiency import group_of
from brieflow_backend import rules_in_comment


@pytest.mark.parametrize(
    "comment, rules",
    [
        ("rule_convert_sbs_wildcards_plate=1", ["convert_sbs"]),
        ("rule_preprocess_sbs_convert_sbs_with_metadata_wildcards_x", ["convert_sbs_with_metadata"]),
        (
            "rule_preprocess_phenotype_convert_phenotype_extract_metadata_phenotype",
            ["extract_metadata_phenotype", "convert_phenotype"],
        ),
        ("rule_sbs_tile_group_align_sbs_segment_sbs", ["segment_sbs", "align_sbs"]),
        ("not_a_rule_comment", []),
    ],
)
def test_group_comments_expand_to_member_rules(comment, rules):
    assert sorted(rules_in_comment(comment)) == sorted(rules)


def test_group_of_maps_preprocess_group_jobs():
    assert group_of("preprocess_sbs_convert_sbs") == "preprocess_sbs"
    assert group_of("convert_sbs") is None


def test_dryrun_preview_reads_rules_under_if_blocks(tmp_path):
    rules = tmp_path / "workflow" / "rules"
    rules.mkdir(parents=True)
    (rules / "preprocess.smk").write_text(
        "rule convert_sbs:\n    script: 'x'\n\nif FOLD:\n\n    rule convert_sbs_with_metadata:\n"
        "        script: 'y'\n"
    )
    assert preview.load_smk_rule_phase(tmp_path) >= 2
    assert preview.rule_phase("convert_sbs_with_metadata") == "preprocess"


def _screen(tmp_path, sizes_itself: bool, obs_source: str):
    analysis = tmp_path / "screen-analysis" / "analysis"
    (analysis / "slurm").mkdir(parents=True)
    smk = tmp_path / "screen-analysis" / "brieflow" / "workflow" / "rules"
    smk.mkdir(parents=True)
    (smk / "preprocess.smk").write_text("mem_mb=get_ic_mem_mb(...)" if sizes_itself else "")
    recs = {
        "convert_sbs": {"mem_mb_recommended": 4000, "obs_source": obs_source},
        "align_sbs": {"mem_mb_recommended": 2500, "obs_source": "observation"},
    }
    (analysis / "slurm" / "mem_recommendations.json").write_text(json.dumps(recs))
    return analysis


def test_resolver_leaves_brieflow_sized_rules_alone(tmp_path):
    resolved = resolver.resolve(_screen(tmp_path, True, "observation"))
    assert "convert_sbs" not in resolved and "calculate_ic_sbs" not in resolved
    assert resolved["align_sbs"]["mem_mb"] == 2500


def test_resolver_keeps_a_post_oom_bump_on_a_brieflow_sized_rule(tmp_path):
    resolved = resolver.resolve(_screen(tmp_path, True, "manual_post_oom"))
    assert resolved["convert_sbs"]["mem_mb"] == 4000


def test_resolver_still_caps_preprocess_on_older_brieflow(tmp_path):
    resolved = resolver.resolve(_screen(tmp_path, False, "observation"))
    assert resolved["convert_sbs"]["mem_mb"] == 4000


def _fake_lib(monkeypatch, reads):
    def read_image(path, cycle=None):
        reads.append((str(path), cycle))
        return np.zeros((2, 4, 4))

    lib = types.ModuleType("lib")
    shared = types.ModuleType("lib.shared")
    file_utils = types.ModuleType("lib.shared.file_utils")
    file_utils.split_well = lambda well: (well[0], well[1:])
    file_utils.get_hcs_nested_path = lambda loc, kind: (
        f"image_{loc['plate']}.zarr/{loc['row']}/{loc['col']}/{loc['tile']}/{loc['cycle']}"
    )
    image_io = types.ModuleType("lib.shared.image_io")
    image_io.read_image = read_image
    for name, mod in {
        "lib": lib,
        "lib.shared": shared,
        "lib.shared.file_utils": file_utils,
        "lib.shared.image_io": image_io,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)


def test_probe_reads_per_cycle_layout(tmp_path, monkeypatch):
    import brieflow_base_order_probe as probe

    reads = []
    _fake_lib(monkeypatch, reads)
    for cycle in (2, 1, 10):
        (tmp_path / "image_1.zarr" / "A" / "1" / "0" / str(cycle)).mkdir(parents=True)
    cycles, imgs = probe.read_preprocess_sbs_cycles(tmp_path, 1, "A1", 0)
    assert cycles == [1, 2, 10] and len(imgs) == 3
    assert [r[1] for r in reads] == [None, None, None]


def test_probe_reads_cycles_on_c_layout(tmp_path, monkeypatch):
    zarr = pytest.importorskip("zarr")
    import brieflow_base_order_probe as probe

    reads = []
    _fake_lib(monkeypatch, reads)
    tile = tmp_path / "image_1.zarr" / "A" / "1" / "0"
    group = zarr.open_group(str(tile), mode="w")
    group.attrs["brieflow"] = {"cycles": ["1", "2", "3"]}
    cycles, imgs = probe.read_preprocess_sbs_cycles(tmp_path, 1, "A1", 0)
    assert cycles == [1, 2, 3] and len(imgs) == 3
    assert [r[1] for r in reads] == ["1", "2", "3"]
