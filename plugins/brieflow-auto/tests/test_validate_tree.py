import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "brieflow_validate_tree.py"
)


@pytest.fixture(scope="module")
def vt():
    spec = importlib.util.spec_from_file_location("brieflow_validate_tree", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["brieflow_validate_tree"] = m
    spec.loader.exec_module(m)
    return m


# --- tree builders -------------------------------------------------------


def _touch(p: Path, n: int = 1):
    p.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (p / f"img_{i}.nd2").write_text("x")


def canonical_tree(root: Path, plates=1, cycles=3, rounds=2):
    """Canonical modality-major: input_<mod>/plate_<N>/<unit>/."""
    for pl in range(1, plates + 1):
        for c in range(1, cycles + 1):
            _touch(root / "input_sbs" / f"plate_{pl}" / f"c{c}")
        for r in range(1, rounds + 1):
            _touch(root / "input_ph" / f"plate_{pl}" / f"round_{r}")
    return root


def plate_major_tree(root: Path, plates=2, cycles=3, rounds=1):
    """Raw plate-major: plate_<N>/input_<mod>/<unit>/ (the GCS convention)."""
    for pl in range(1, plates + 1):
        for c in range(1, cycles + 1):
            _touch(root / f"plate_{pl}" / "input_sbs" / f"c{c}")
        for r in range(1, rounds + 1):
            _touch(root / f"plate_{pl}" / "input_ph" / f"round_{r}")
    return root


# --- canonical / idempotence --------------------------------------------


def test_canonical_tree_is_clean(vt, tmp_path):
    root = canonical_tree(tmp_path / "screen")
    plan = vt.plan_reshape(root)
    assert plan.is_canonical
    assert plan.ops == []
    assert plan.blockers == []


def test_apply_on_canonical_is_noop(vt, tmp_path):
    root = canonical_tree(tmp_path / "screen")
    res = vt.apply_plan(root)
    assert res["applied"] is False
    assert res["blocked"] is False
    assert vt.plan_reshape(root).is_canonical


def test_sidecars_are_ignored(vt, tmp_path):
    """`_`-prefixed sidecars (parked data) don't perturb a canonical tree —
    this is what makes --apply idempotent across a GCS re-pull."""
    root = canonical_tree(tmp_path / "screen")
    _touch(root / "_excluded_cycles" / "plate_1" / "c12")
    _touch(root / "_non_image" / "input_sbs" / "plate_1" / "junk")
    (root / "_config").mkdir()
    plan = vt.plan_reshape(root)
    assert plan.is_canonical


# --- alias renames -------------------------------------------------------


def test_sbs_ph_aliases_renamed(vt, tmp_path):
    root = tmp_path / "screen"
    _touch(root / "sbs" / "plate_1" / "c1")
    _touch(root / "ph" / "plate_1" / "round_1")
    plan = vt.plan_reshape(root)
    dsts = {Path(o.dst).name for o in plan.ops if o.op == "rename"}
    assert "input_sbs" in dsts and "input_ph" in dsts
    assert plan.blockers == []


@pytest.mark.parametrize("alias,canonical", [("cycle_1", "c1"), ("C1", "c1"), ("cycle1", "c1")])
def test_cycle_alias_variants(vt, tmp_path, alias, canonical):
    root = tmp_path / "screen"
    _touch(root / "input_sbs" / "plate_1" / alias)
    _touch(root / "input_ph" / "plate_1" / "round_1")
    plan = vt.plan_reshape(root)
    renamed = {Path(o.dst).name for o in plan.ops if o.op == "rename"}
    assert canonical in renamed


@pytest.mark.parametrize("alias", ["r1", "round1", "Round_1"])
def test_round_alias_variants(vt, tmp_path, alias):
    root = tmp_path / "screen"
    _touch(root / "input_sbs" / "plate_1" / "c1")
    _touch(root / "input_ph" / "plate_1" / alias)
    plan = vt.plan_reshape(root)
    renamed = {Path(o.dst).name for o in plan.ops if o.op == "rename"}
    assert "round_1" in renamed


@pytest.mark.parametrize("alias", ["plate1", "Plate_1", "p1"])
def test_plate_alias_variants(vt, tmp_path, alias):
    root = tmp_path / "screen"
    _touch(root / "input_sbs" / alias / "c1")
    _touch(root / "input_ph" / alias / "round_1")
    plan = vt.plan_reshape(root)
    renamed = {Path(o.dst).name for o in plan.ops if o.op == "rename"}
    assert "plate_1" in renamed


# --- wrap (missing level for a single unit) ------------------------------


def test_single_round_wraps_into_round_1(vt, tmp_path):
    root = tmp_path / "screen"
    _touch(root / "input_sbs" / "plate_1" / "c1")
    _touch(root / "input_ph" / "plate_1", n=3)  # loose images, no round dir
    plan = vt.plan_reshape(root)
    wraps = [o for o in plan.ops if o.op == "wrap"]
    assert any(Path(o.dst).name == "round_1" for o in wraps)
    assert plan.blockers == []


def test_single_plate_wraps_into_plate_1(vt, tmp_path):
    root = tmp_path / "screen"
    # no plate_* level — units hang directly under the modality anchor
    _touch(root / "input_sbs" / "c1")
    _touch(root / "input_ph" / "round_1")
    plan = vt.plan_reshape(root)
    assert any(o.op == "wrap" and Path(o.dst).name == "plate_1" for o in plan.ops)


def test_loose_sbs_images_across_cycles_block_not_wrap(vt, tmp_path):
    root = tmp_path / "screen"
    _touch(root / "input_sbs", n=4)  # cycles only in file names
    _touch(root / "input_ph" / "plate_1" / "round_1")
    plan = vt.plan_reshape(root, declared_cycles=12)
    assert [b.code for b in plan.blockers] == ["cycle-unseparated"]
    assert not any(o.op == "wrap" and "input_sbs" in o.src for o in plan.ops)
    res = vt.apply_plan(root, declared_cycles=12)
    assert res == {"applied": False, "blocked": True, "passes": 0, "warnings": []}
    assert not (root / "input_sbs" / "plate_1").exists()


def test_loose_sbs_images_wrap_when_one_cycle_declared(vt, tmp_path):
    root = tmp_path / "screen"
    _touch(root / "input_sbs" / "plate_1", n=2)
    _touch(root / "input_ph" / "plate_1" / "round_1")
    plan = vt.plan_reshape(root, declared_cycles=1)
    assert plan.blockers == []
    assert any(o.op == "wrap" and Path(o.dst).name == "c1" for o in plan.ops)


def test_images_in_non_unit_subdirs_block_before_any_move(vt, tmp_path):
    root = tmp_path / "screen"
    _touch(root / "input_sbs" / "real_images", n=3)
    _touch(root / "input_ph" / "real_images", n=1)
    plan = vt.plan_reshape(root, declared_cycles=3)
    assert sorted(b.code for b in plan.blockers) == ["cycle-unseparated", "round-unseparated"]
    assert plan.ops == []


def test_plate_major_unseparated_cycles_block(vt, tmp_path):
    root = tmp_path / "screen"
    _touch(root / "plate_1" / "input_sbs", n=2)
    _touch(root / "plate_1" / "input_ph" / "round_1")
    plan = vt.plan_reshape(root, declared_cycles=2)
    assert [b.code for b in plan.blockers] == ["cycle-unseparated"]


# --- plate-major → modality-major transpose ------------------------------


def test_plate_major_is_not_canonical(vt, tmp_path):
    root = plate_major_tree(tmp_path / "screen")
    plan = vt.plan_reshape(root)
    assert not plan.is_canonical
    assert any(o.op == "move" for o in plan.ops)
    assert plan.blockers == []


def test_apply_transposes_plate_major(vt, tmp_path):
    root = plate_major_tree(tmp_path / "screen", plates=2, cycles=3, rounds=1)
    res = vt.apply_plan(root)
    assert res["applied"] is True
    assert res["blocked"] is False
    assert vt.plan_reshape(root).is_canonical
    assert (root / "input_sbs" / "plate_1" / "c1").is_dir()
    assert (root / "input_sbs" / "plate_2" / "c3").is_dir()
    assert (root / "input_ph" / "plate_1" / "round_1").is_dir()
    # the original plate-major dirs are pruned away
    assert not (root / "plate_1").exists()
    assert not (root / "plate_2").exists()


def test_apply_transposes_aliased_plate_major(vt, tmp_path):
    root = tmp_path / "screen"
    _touch(root / "Plate_1" / "sbs" / "cycle_1", n=2)
    _touch(root / "Plate_1" / "sbs" / "cycle_2", n=2)
    _touch(root / "Plate_1" / "ph", n=4)  # single round, loose
    res = vt.apply_plan(root)
    assert res["applied"] is True and res["blocked"] is False
    assert vt.plan_reshape(root).is_canonical
    assert (root / "input_sbs" / "plate_1" / "c1").is_dir()
    assert (root / "input_sbs" / "plate_1" / "c2").is_dir()
    assert len(list((root / "input_ph" / "plate_1" / "round_1").glob("*.nd2"))) == 4


# --- cycle census + parking ----------------------------------------------


def test_surplus_cycles_parked(vt, tmp_path):
    root = tmp_path / "screen"
    for c in range(1, 14):  # c1..c13
        _touch(root / "input_sbs" / "plate_1" / f"c{c}")
    _touch(root / "input_ph" / "plate_1" / "round_1")
    plan = vt.plan_reshape(root, declared_cycles=11)
    parks = [o for o in plan.ops if o.op == "park"]
    parked_names = {Path(o.dst).name for o in parks}
    assert "c12" in parked_names and "c13" in parked_names
    assert plan.blockers == []


def test_apply_parks_surplus_cycles(vt, tmp_path):
    root = tmp_path / "screen"
    for c in range(1, 14):
        _touch(root / "input_sbs" / "plate_1" / f"c{c}")
    _touch(root / "input_ph" / "plate_1" / "round_1")
    res = vt.apply_plan(root, declared_cycles=11)
    assert res["blocked"] is False
    assert vt.plan_reshape(root, declared_cycles=11).is_canonical
    assert (root / "input_sbs" / "plate_1" / "c11").is_dir()
    assert not (root / "input_sbs" / "plate_1" / "c12").exists()
    assert (root / "_excluded_cycles" / "plate_1" / "c12").is_dir()
    assert (root / "_excluded_cycles" / "plate_1" / "c13").is_dir()


def test_junk_dir_parked(vt, tmp_path):
    root = tmp_path / "screen"
    _touch(root / "input_sbs" / "plate_1" / "c1")
    (root / "input_sbs" / "plate_1" / "Dont use").mkdir(parents=True)
    _touch(root / "input_ph" / "plate_1" / "round_1")
    plan = vt.plan_reshape(root)
    parks = [o for o in plan.ops if o.op == "park"]
    assert any("Dont use" in o.src for o in parks)
    res = vt.apply_plan(root)
    assert res["blocked"] is False
    assert not (root / "input_sbs" / "plate_1" / "Dont use").exists()
    assert (root / "_non_image" / "input_sbs" / "plate_1" / "Dont use").is_dir()


def test_nonuniform_cycle_counts_warns(vt, tmp_path):
    root = tmp_path / "screen"
    for c in (1, 2, 3):
        _touch(root / "input_sbs" / "plate_1" / f"c{c}")
    for c in (1, 2):
        _touch(root / "input_sbs" / "plate_2" / f"c{c}")
    _touch(root / "input_ph" / "plate_1" / "round_1")
    _touch(root / "input_ph" / "plate_2" / "round_1")
    plan = vt.plan_reshape(root)
    assert plan.warnings  # non-uniform cycle counts flagged
    assert plan.blockers == []  # but not a blocker


# --- blockers ------------------------------------------------------------


def test_cycle_gap_is_blocker(vt, tmp_path):
    root = tmp_path / "screen"
    for c in (1, 2, 4):  # missing c3, below any declared count
        _touch(root / "input_sbs" / "plate_1" / f"c{c}")
    _touch(root / "input_ph" / "plate_1" / "round_1")
    plan = vt.plan_reshape(root)
    assert any("gap" in b.code for b in plan.blockers)


def test_collision_is_blocker(vt, tmp_path):
    root = tmp_path / "screen"
    _touch(root / "input_sbs" / "plate_1" / "c1")
    _touch(root / "input_sbs" / "plate_1" / "cycle_1")  # both → c1
    _touch(root / "input_ph" / "plate_1" / "round_1")
    plan = vt.plan_reshape(root)
    assert any("collision" in b.code for b in plan.blockers)


def test_missing_sbs_is_blocker(vt, tmp_path):
    root = tmp_path / "screen"
    _touch(root / "input_ph" / "plate_1" / "round_1")  # no input_sbs
    plan = vt.plan_reshape(root)
    assert any("missing" in b.code for b in plan.blockers)


def test_ambiguous_root_is_blocker(vt, tmp_path):
    root = tmp_path / "screen"
    _touch(root / "input_sbs" / "plate_1" / "c1")  # modality-major
    _touch(root / "plate_1" / "input_sbs" / "c1")  # AND plate-major
    plan = vt.plan_reshape(root)
    assert any(b.code == "ambiguous-root" for b in plan.blockers)


def test_apply_refuses_when_blocked(vt, tmp_path):
    root = tmp_path / "screen"
    for c in (1, 2, 4):
        _touch(root / "input_sbs" / "plate_1" / f"c{c}")
    _touch(root / "input_ph" / "plate_1" / "round_1")
    res = vt.apply_plan(root)
    assert res["blocked"] is True
    assert res["applied"] is False
    assert (root / "input_sbs" / "plate_1" / "c4").is_dir()  # untouched


# --- serialization + helpers ---------------------------------------------


def test_plan_to_dict_serializable(vt, tmp_path):
    import json

    root = tmp_path / "screen"
    _touch(root / "sbs" / "plate_1" / "c1")
    _touch(root / "input_ph" / "plate_1" / "round_1")
    d = vt.plan_reshape(root).to_dict()
    json.dumps(d)
    assert d["root"] == str(root)
    assert "ops" in d and "blockers" in d and "warnings" in d


def test_rsync_filter_lines(vt):
    lines = vt.rsync_filter_lines()
    blob = "\n".join(lines)
    assert "_excluded_cycles" in blob
    assert "_non_image" in blob


def test_census_tree_reports_anomalies(vt, tmp_path):
    """census_tree on a modality-major tree with a surplus cycle + a junk dir
    surfaces the facts verify_notes_against_data reconciles against."""
    root = tmp_path / "screen"
    for c in range(1, 13):  # plate_1: c1..c12 (one over a declared 11)
        _touch(root / "input_sbs" / "plate_1" / f"c{c}")
    for c in range(1, 12):  # plate_2: c1..c11
        _touch(root / "input_sbs" / "plate_2" / f"c{c}")
    (root / "input_sbs" / "plate_1" / "Dont use").mkdir(parents=True)
    _touch(root / "input_ph" / "plate_1" / "round_1")
    _touch(root / "input_ph" / "plate_2" / "round_1")

    cen = vt.census_tree(root, declared_cycles=11)
    assert cen["plates"] == [1, 2]
    assert cen["cycles_per_plate"] == {1: 12, 2: 11}
    assert cen["max_cycles"] == 12
    assert cen["uniform_cycles"] is False
    assert cen["declared_cycles"] == 11
    assert cen["surplus_cycles"] == {1: 1}
    assert any("Dont use" in j for j in cen["junk_dirs"])


def test_declared_cycles_from_screen_yaml(vt, tmp_path):
    pytest.importorskip("yaml")
    sy = tmp_path / "screen.yaml"
    sy.write_text("iss:\n  cycles: 11\n")
    assert vt.declared_cycles_from_screen_yaml(sy) == 11
    sy2 = tmp_path / "noiss.yaml"
    sy2.write_text("data:\n  foo: bar\n")
    assert vt.declared_cycles_from_screen_yaml(sy2) is None


# --- multi-round phenotype + one acquisition per unit ----------------------


def test_two_acquisitions_in_one_round_block(vt, tmp_path):
    root = canonical_tree(tmp_path / "screen", rounds=1)
    rnd = root / "input_ph" / "plate_1" / "round_1"
    for m in ("Measurement 1", "Measurement 2"):
        _touch(rnd / m / "Images", n=2)  # same file names in both exports
    plan = vt.plan_reshape(root)
    assert "round-duplicate-acquisition" in [b.code for b in plan.blockers]


def test_phenix_images_subfolder_is_one_acquisition(vt, tmp_path):
    root = canonical_tree(tmp_path / "screen", rounds=1)
    rnd = root / "input_ph" / "plate_1" / "round_1"
    (rnd / "img_0.nd2").unlink()
    _touch(rnd / "Images", n=3)
    assert vt.plan_reshape(root).blockers == []


def test_empty_cycle_dir_blocks(vt, tmp_path):
    root = canonical_tree(tmp_path / "screen", cycles=2)
    (root / "input_sbs" / "plate_1" / "c3").mkdir()
    plan = vt.plan_reshape(root, declared_cycles=3)
    assert "cycle-unit-empty" in [b.code for b in plan.blockers]


def test_plates_with_different_rounds_block(vt, tmp_path):
    root = canonical_tree(tmp_path / "screen", plates=2, rounds=2)
    import shutil
    shutil.rmtree(root / "input_ph" / "plate_2" / "round_2")
    plan = vt.plan_reshape(root)
    assert "round-plate-mismatch" in [b.code for b in plan.blockers]


def test_uneven_rounds_need_confirmation(vt, tmp_path, monkeypatch, capsys):
    root = canonical_tree(tmp_path / "screen", rounds=2)
    _touch(root / "input_ph" / "plate_1" / "round_2", n=3)
    plan = vt.plan_reshape(root)
    assert plan.blockers == [] and plan.ops == []
    assert any("round_1: 1, round_2: 3" in w for w in plan.warnings)
    monkeypatch.setattr(sys, "argv", ["vt", "--root", str(root)])
    assert vt.main() == 3
    assert "CONFIRM WITH THE OPERATOR" in capsys.readouterr().out


def test_layout_flag_prints_contract(vt, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["vt", "--layout"])
    assert vt.main() == 0
    out = capsys.readouterr().out
    assert "round_2/" in out and "c<iss.cycles>" in out


def test_validate_inputs_reads_the_screen_yaml_dirs(vt, tmp_path):
    root = canonical_tree(tmp_path / "screen", rounds=2)
    sbs, ph = tmp_path / "raw_sbs", tmp_path / "raw_pheno"
    (root / "input_sbs").rename(sbs)
    (root / "input_ph").rename(ph)
    assert vt.validate_inputs(sbs, ph, 3).is_canonical
    _touch(ph / "plate_1", n=2)  # loose images beside round dirs
    assert "round-mixed" in [b.code for b in vt.validate_inputs(sbs, ph, 3).blockers]
    assert "missing-cycle" in [b.code for b in vt.validate_inputs(tmp_path / "nope", ph, 3).blockers]


def test_inputs_from_screen_yaml(vt, tmp_path, monkeypatch):
    root = canonical_tree(tmp_path / "data", rounds=2)
    sy = tmp_path / "analysis" / "screen.yaml"
    sy.parent.mkdir()
    sy.write_text(
        f"iss: {{cycles: 3}}\ndata: {{input_sbs_dir: {root / 'input_sbs'}, "
        "input_phenotype_dir: ../data/input_ph}\n"
    )
    assert vt.validate_screen_inputs(sy).is_canonical
    monkeypatch.setattr(sys, "argv", ["vt", "--inputs-from", str(sy)])
    assert vt.main() == 0
    sy.write_text("iss: {cycles: 3}\ndata: {}\n")
    assert "no-input-dirs" in [b.code for b in vt.validate_screen_inputs(sy).blockers]
