#!/usr/bin/env python3
r"""
brieflow_validate_tree.py — OPS raw-data filetree validator + reshaper.

Once a screen's raw data lands on the VM (gcloud storage rsync → /data),
the directory tree must match brieflow's canonical **modality-major**
hierarchy before the pipeline can run:

    <screen>/
      input_sbs/
        plate_<N>/                N = 1..P, contiguous
          c<M>/                   M = 1..C, contiguous (sequencing cycles)
            <image files>
      input_ph/
        plate_<N>/                N = 1..P, contiguous
          round_<M>/              M = 1..R, contiguous (phenotype rounds)
            <image files>

`screen.yaml`'s data.input_sbs_dir / input_phenotype_dir point at the
input_sbs/ and input_ph/ anchors, so brieflow's `plate_(\d+)/c(\d+)/…`
regex runs beneath them. This is the layout damavand ran end-to-end. (An
earlier version of this tool targeted the *opposite*, plate-major shape —
`plate_<N>/input_sbs/c<M>/` — which would reshape working data the wrong
way; that was a bug.)

Real data rarely lands like this:

  - It often arrives **plate-major** (`plate_<N>/input_sbs/c<M>/…`) — the
    raw GCS convention — and must be **transposed** to modality-major.
  - Folders come aliased: `sbs`/`ph`, `cycle_1`/`C1`, `Plate_1`, `r1`.
  - A single phenotype round sits loose under input_ph/ with no `round_1/`
    wrapper; a single plate has no `plate_*/` level at all.
  - Junk dirs ("Dont use") and stray non-image files sit in the regex path.
  - More sequencing cycles are present than the screen actually used.

This helper walks the tree and proposes a reshape plan of op kinds:

  - rename: an alias dir → its canonical name in place (sbs→input_sbs,
            cycle_1→c1, Plate_1→plate_1, r1→round_1).
  - move:   relocate a subtree to its canonical path — the plate-major →
            modality-major transpose (`plate_<N>/input_sbs` → `input_sbs/
            plate_<N>`).
  - wrap:   a missing single-unit level — mkdir the canonical child and
            sweep loose image files into it (loose phenotype images →
            round_1/; input_sbs/input_ph with no plate level → plate_1/).
  - park:   move data that must leave the regex path into a `_`-prefixed
            sidecar: surplus cycles beyond the declared count
            (`_excluded_cycles/plate_<N>/c<M>/`), non-cycle junk dirs and
            stray non-image files (`_non_image/…`).
  - prune:  rmdir an emptied plate/alias dir left behind by a transpose.

Cycle census: with the declared cycle count (``--cycles N`` or read from
``--screen-yaml`` as ``iss.cycles``), sequencing cycles above the declared
count are parked, and non-uniform per-plate cycle counts are flagged
loudly — a WARNING, not a blocker (the parking makes the kept set uniform).

Anything it can't map unambiguously is a BLOCKER (index gaps like c1,c2,c4
*at or below* the declared count; two dirs mapping to one canonical name; a
plate missing input_sbs or input_ph; image files loose alongside indexed
unit dirs). Blockers are never auto-fixed — the operator resolves them.

Idempotence + re-pull durability: every `_`-prefixed dir
(`_excluded_cycles`, `_non_image`, `_config`, `_reshape_manifests`, …) is
ignored by the planner, so a re-run after a GCS re-pull converges and
``--apply`` is safe to run repeatedly. ``--rsync-filter`` emits a filter
file the pull can honor (``gcloud storage rsync`` / ``rsync``) so a re-pull
does not re-introduce parked or reshaped-away data.

Default run is dry-run: prints the plan (human-readable, or --json) and
exits 0 if canonical, 2 if a reshape is pending, 1 if blocked, 3 if canonical
but something needs the operator's confirmation (non-uniform cycle counts, phenotype
rounds of different sizes). `--layout` prints the expected structure. With --apply
it executes the plan (iterating to a fixpoint so nested renames, moves and
wraps land in a safe order), then re-validates. --apply refuses to touch
anything while any blocker is present.

Usage:
    brieflow_validate_tree.py --layout
    brieflow_validate_tree.py --inputs-from analysis/screen.yaml
    brieflow_validate_tree.py --root /data/<screen>
    brieflow_validate_tree.py --root /data/<screen> --screen-yaml .../screen.yaml
    brieflow_validate_tree.py --root /data/<screen> --cycles 11 --json
    brieflow_validate_tree.py --root /data/<screen> --cycles 11 --apply
    brieflow_validate_tree.py --root /data/<screen> --rsync-filter rsync_filter.txt
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_PASSES = 16  # fixpoint guard for apply_plan

# Dirs whose name starts with this are sidecars (parked / config / manifests)
# and are invisible to the planner — this is what makes --apply idempotent.
PARK_PREFIX = "_"
EXCLUDED_CYCLES_DIR = "_excluded_cycles"
NON_IMAGE_DIR = "_non_image"

# Microscope image file extensions. Anything else loose in the regex path is
# parked to _non_image/ rather than swept into a cycle/round.
IMAGE_EXTS = {".nd2", ".tif", ".tiff", ".czi", ".ims"}

# The raw-data layout brieflow reads. `--layout` prints this; setup, the runner and the
# operator-facing skills point here rather than restating it.
EXPECTED_LAYOUT = """\
Expected raw-data layout (brieflow reads the cycle and round from the directory, never
from a file name):

  <data root>/
    input_sbs/                    screen.yaml data.input_sbs_dir points here
      plate_1/
        c1/   <image files>       one directory per SBS cycle: c1 .. c<iss.cycles>
        c2/   <image files>
        ...
      plate_2/ ...                plates numbered 1..P with no gaps
    input_ph/                     screen.yaml data.input_phenotype_dir points here
      plate_1/
        round_1/  <image files>   one directory per phenotype imaging round
        round_2/  <image files>   (multi-round screens only)
      plate_2/ ...

Rules:
  - One acquisition per cycle or round directory. A vendor subfolder such as Images/
    (Opera Phenix) is fine; two exports of the same fields in one directory are not.
  - Every plate has the same phenotype rounds, and every round images the same wells
    and fields. Round numbers follow acquisition order.
  - Multi-round phenotype: each round directory holds only the channels imaged in that
    pass. screen.yaml phenotype.channels lists every channel of every round, round 1
    first, in file order within each round.
  - A single-round screen still uses round_1/; setup wraps loose phenotype images into it.
  - Files that are not images (.xml, .csv, Evaluation folders) may sit beside the images;
    setup parks anything that would confuse the file patterns.
"""


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------


@dataclass
class Op:
    op: str  # "rename" | "move" | "wrap" | "park" | "prune"
    src: str
    dst: str
    reason: str = ""

    def to_dict(self) -> dict:
        return {"op": self.op, "src": self.src, "dst": self.dst, "reason": self.reason}


@dataclass
class Blocker:
    path: str
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass
class ReshapePlan:
    root: str
    ops: list[Op] = field(default_factory=list)
    blockers: list[Blocker] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def is_canonical(self) -> bool:
        # warnings (e.g. non-uniform cycle counts) do not block — they are
        # advisory and the data is still runnable after any parking.
        return not self.ops and not self.blockers

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "is_canonical": self.is_canonical,
            "ops": [o.to_dict() for o in self.ops],
            "blockers": [b.to_dict() for b in self.blockers],
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Canonical-name matchers — return the index for indexed levels, or a bool.
# Bare-numeric dirs (`1`, `2`) are accepted contextually: a child of input_sbs
# is a cycle, a child of input_ph is a round.
# ---------------------------------------------------------------------------


def _match_plate(name: str) -> int | None:
    m = re.fullmatch(r"plate[_-]?(\d+)|p(\d+)", name, re.IGNORECASE)
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def _match_cycle(name: str) -> int | None:
    m = re.fullmatch(r"c(?:ycle)?[_-]?(\d+)|(\d+)", name, re.IGNORECASE)
    return int(m.group(1) or m.group(2)) if m else None


def _match_round(name: str) -> int | None:
    m = re.fullmatch(r"r(?:ound)?[_-]?(\d+)|(\d+)", name, re.IGNORECASE)
    return int(m.group(1) or m.group(2)) if m else None


def _is_sbs(name: str) -> bool:
    return re.fullmatch(r"(?:input[_-]?)?sbs", name, re.IGNORECASE) is not None


def _is_ph(name: str) -> bool:
    return (
        re.fullmatch(r"(?:input[_-]?)?(?:ph|phenotype)", name, re.IGNORECASE)
        is not None
    )


# ---------------------------------------------------------------------------
# Tree inspection helpers
# ---------------------------------------------------------------------------


def _subdirs(p: Path) -> list[Path]:
    """Visible child dirs, sorted by name. Sidecars (`_`-prefixed) and hidden
    dot-dirs (.ipynb_checkpoints, .git) are invisible to the planner — the
    former is what makes --apply idempotent; the latter are never data."""
    return sorted(
        (
            c
            for c in p.iterdir()
            if c.is_dir()
            and not c.name.startswith(PARK_PREFIX)
            and not c.name.startswith(".")
        ),
        key=lambda c: c.name,
    )


def _loose_files(p: Path) -> list[Path]:
    return [c for c in p.iterdir() if c.is_file() and not c.name.startswith(".")]


def _is_image(p: Path) -> bool:
    return p.suffix.lower() in IMAGE_EXTS


def _dir_empty(p: Path) -> bool:
    """True if a dir has no entries at all (used to prune transpose leftovers)."""
    return not any(p.iterdir())


def _rename_op(d: Path, canonical: str, reason: str) -> Op | None:
    """Emit a rename op only when the dir isn't already canonically named."""
    if d.name == canonical:
        return None
    return Op("rename", src=str(d), dst=str(d.parent / canonical), reason=reason)


def _park_op(root: Path, src: Path, area: str, reason: str) -> Op:
    """Park `src` under `<root>/<area>/<src-relative-to-root>` to preserve
    provenance (e.g. _non_image/input_sbs/plate_2/Dont use)."""
    try:
        rel = src.relative_to(root)
    except ValueError:
        rel = Path(src.name)
    return Op("park", src=str(src), dst=str(root / area / rel), reason=reason)


def _image_files(d: Path) -> list[Path]:
    """Image files anywhere below `d`, skipping sidecar and hidden dirs."""
    found = []
    for dirpath, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if not x.startswith((PARK_PREFIX, "."))]
        found.extend(Path(dirpath) / f for f in files if Path(f).suffix.lower() in IMAGE_EXTS)
    return found


def _has_image(d: Path) -> bool:
    """True as soon as one image file is found below `d` (stops at the first)."""
    for dirpath, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if not x.startswith((PARK_PREFIX, "."))]
        if any(Path(f).suffix.lower() in IMAGE_EXTS for f in files):
            return True
    return False


def _check_unit_contents(unit: Path, kind: str, plan: ReshapePlan) -> None:
    """A cycle/round dir must hold images, from one acquisition: the same file name in two
    of its subfolders means two exports of the same fields (e.g. two Phenix Measurements).
    Only a unit with two or more image-bearing subfolders is listed in full."""
    if not _has_image(unit):
        plan.blockers.append(
            Blocker(str(unit), f"{kind}-unit-empty", f"{unit.name} holds no image files")
        )
        return
    branches = [d for d in _subdirs(unit) if _has_image(d)]
    if any(_is_image(f) for f in _loose_files(unit)):
        branches.append(unit)
    if len(branches) < 2:
        return
    seen: dict[str, Path] = {}
    for branch in branches:
        names = (
            {f.name for f in _loose_files(unit) if _is_image(f)}
            if branch == unit
            else {f.name for f in _image_files(branch)}
        )
        for name in names:
            if name in seen:
                plan.blockers.append(
                    Blocker(
                        str(unit),
                        f"{kind}-duplicate-acquisition",
                        f"{unit.name} holds the same file name in two folders "
                        f"({seen[name].name} and {branch.name}, e.g. {name}): two acquisitions "
                        f"in one {kind}. Keep the one the screen uses; a further phenotype "
                        "round goes in its own round_<R>/",
                    )
                )
                return
            seen[name] = branch


def _check_round_consistency(anchor: Path, plan: ReshapePlan) -> None:
    """Multi-round phenotype data: every plate carries the same rounds, and a round whose
    image count differs from the others on its plate is put to the operator."""
    plates = {_match_plate(p.name): p for p in _subdirs(anchor) if _match_plate(p.name)}
    rounds = {
        i: {_match_round(r.name): r for r in _subdirs(p) if _match_round(r.name) is not None}
        for i, p in plates.items()
    }
    round_sets = {i: sorted(r) for i, r in rounds.items() if r}
    if len({tuple(v) for v in round_sets.values()}) > 1:
        plan.blockers.append(
            Blocker(
                str(anchor),
                "round-plate-mismatch",
                "plates carry different phenotype rounds: "
                + ", ".join(f"plate_{i}: {v}" for i, v in sorted(round_sets.items()))
                + " — every plate needs the same rounds",
            )
        )
    for i, by_round in sorted(rounds.items()):
        if len(by_round) < 2:
            continue
        counts = {r: len(_image_files(d)) for r, d in sorted(by_round.items())}
        if len(set(counts.values())) > 1:
            plan.warnings.append(
                f"plate_{i}: phenotype rounds hold different numbers of image files "
                f"({', '.join(f'round_{r}: {n}' for r, n in counts.items())}). Fine when "
                "rounds image different channel counts as separate files; otherwise a round "
                "is missing wells or fields, or holds an extra acquisition — confirm with "
                "the operator"
            )


def _check_index_contiguity(
    parent: Path, dirs: list[Path], idx_of, kind: str, plan: ReshapePlan
) -> None:
    """Flag non-contiguous indices (e.g. plate_1, plate_3 missing plate_2)."""
    indices = sorted({idx_of(d.name) for d in dirs if idx_of(d.name) is not None})
    if not indices:
        return
    expected = list(range(1, max(indices) + 1))
    missing = sorted(set(expected) - set(indices))
    if missing:
        plan.blockers.append(
            Blocker(
                str(parent),
                f"{kind}-gap",
                f"non-contiguous {kind} indices under {parent.name}: "
                f"have {indices}, missing {missing}",
            )
        )


# ---------------------------------------------------------------------------
# Unit-level planner (cycles under input_sbs/plate, rounds under input_ph/plate)
# ---------------------------------------------------------------------------


def _plan_units(
    root: Path,
    plate_dir: Path,
    plate_idx: int,
    kind: str,
    idx_of,
    canonical,
    wrap_name: str,
    declared_cycles: int | None,
    plan: ReshapePlan,
) -> int | None:
    """Plan one `input_sbs/plate_<N>` or `input_ph/plate_<N>` dir. Returns the
    count of kept indexed units (for the non-uniformity check), or None."""
    dirs = _subdirs(plate_dir)
    indexed = [d for d in dirs if idx_of(d.name) is not None]
    junk = [d for d in dirs if idx_of(d.name) is None]
    loose = _loose_files(plate_dir)

    # Park non-cycle/non-round junk dirs (e.g. literal "Dont use" dirs).
    for d in junk:
        plan.ops.append(
            _park_op(
                root,
                d,
                NON_IMAGE_DIR,
                f"non-{kind} dir in regex path: {d.name!r}",
            )
        )

    if indexed:
        # Loose files alongside indexed unit dirs: park non-images; image files
        # are ambiguous (which unit?) → blocker.
        for f in loose:
            if _is_image(f):
                plan.blockers.append(
                    Blocker(
                        str(plate_dir),
                        f"{kind}-mixed",
                        f"{plate_dir.name} has both {kind} unit dirs and loose "
                        f"image file {f.name!r}",
                    )
                )
            else:
                plan.ops.append(
                    _park_op(root, f, NON_IMAGE_DIR, "stray non-image file")
                )

        by_index: dict[int, list[Path]] = {}
        for d in indexed:
            by_index.setdefault(idx_of(d.name), []).append(d)

        # Collisions (two dirs → same index).
        collided = set()
        for i, group in by_index.items():
            if len(group) > 1:
                collided.add(i)
                plan.blockers.append(
                    Blocker(
                        str(plate_dir),
                        f"{kind}-collision",
                        f"{len(group)} dirs map to {canonical(i)}: "
                        + ", ".join(g.name for g in group),
                    )
                )

        present = sorted(i for i in by_index if i not in collided)

        # Census: park sequencing cycles above the declared count.
        if kind == "cycle" and declared_cycles:
            surplus = [i for i in present if i > declared_cycles]
            for i in surplus:
                d = by_index[i][0]
                plan.ops.append(
                    Op(
                        "park",
                        src=str(d),
                        dst=str(root / EXCLUDED_CYCLES_DIR / f"plate_{plate_idx}" / f"c{i}"),
                        reason=f"cycle {i} > declared iss.cycles={declared_cycles}",
                    )
                )
            present = [i for i in present if i <= declared_cycles]

        # Rename kept alias dirs → canonical.
        for i in present:
            op = _rename_op(by_index[i][0], canonical(i), f"{kind} alias")
            if op:
                plan.ops.append(op)

        for i in present:
            _check_unit_contents(by_index[i][0], kind, plan)

        # Contiguity on the kept set (a gap below declared is a real blocker).
        if present:
            expected = list(range(1, max(present) + 1))
            missing = sorted(set(expected) - set(present))
            if missing:
                plan.blockers.append(
                    Blocker(
                        str(plate_dir),
                        f"{kind}-gap",
                        f"non-contiguous {kind} indices under {plate_dir.name}: "
                        f"have {present}, missing {missing}",
                    )
                )
        return len(present)

    # No indexed unit dirs — a single unit whose files sit loose.
    images = [f for f in loose if _is_image(f)]
    non_images = [f for f in loose if not _is_image(f)]
    if non_images:
        # Park stray non-images first; the wrap is deferred to the next pass so
        # it only sweeps real image files into the single canonical unit.
        for f in non_images:
            plan.ops.append(_park_op(root, f, NON_IMAGE_DIR, "stray non-image file"))
        return None
    if images:
        plan.ops.append(
            Op(
                "wrap",
                src=str(plate_dir),
                dst=str(plate_dir / wrap_name),
                reason=f"single {kind}: no {wrap_name}/ wrapper",
            )
        )
        return 1
    plan.blockers.append(
        Blocker(str(plate_dir), f"{kind}-empty", f"{plate_dir.name} has no {kind} data")
    )
    return 0


# ---------------------------------------------------------------------------
# Modality-anchor planner (input_sbs/ or input_ph/ already at root)
# ---------------------------------------------------------------------------


def _plan_modality_anchor(
    root: Path, anchor: Path, kind: str, idx_of, canonical, wrap_name: str,
    declared_cycles: int | None, plan: ReshapePlan,
) -> dict[int, int]:
    """Plan one canonical modality dir: ensure plate_<N> level, then units.
    Returns {plate_idx: kept_unit_count}."""
    children = _subdirs(anchor)
    plate_dirs = [d for d in children if _match_plate(d.name) is not None]
    non_plate = [d for d in children if _match_plate(d.name) is None]

    counts: dict[int, int] = {}
    if plate_dirs:
        _check_index_contiguity(anchor, plate_dirs, _match_plate, "plate", plan)
        # Park non-plate junk dirs sitting at the modality level.
        for d in non_plate:
            plan.ops.append(
                _park_op(root, d, NON_IMAGE_DIR, f"non-plate dir under {anchor.name}")
            )
        for d in list(plate_dirs):
            i = _match_plate(d.name)
            op = _rename_op(d, f"plate_{i}", "plate alias")
            if op:
                plan.ops.append(op)
            n = _plan_units(
                root, d, i, kind, idx_of, canonical, wrap_name, declared_cycles, plan
            )
            if n is not None:
                counts[i] = n
        return counts

    # No plate level under the modality anchor.
    loose = _loose_files(anchor)
    if not non_plate and not loose:
        plan.blockers.append(
            Blocker(str(anchor), f"{kind}-anchor-empty", f"{anchor.name} is empty")
        )
        return counts
    # Single plate: wrap the unit dirs / loose files into plate_1.
    plan.ops.append(
        Op(
            "wrap",
            src=str(anchor),
            dst=str(anchor / "plate_1"),
            reason=f"single plate: no plate_*/ under {anchor.name}",
        )
    )
    return counts


# ---------------------------------------------------------------------------
# Top-level planner
# ---------------------------------------------------------------------------


def unseparated_blocker(
    anchor: Path, kind: str, idx_of, declared_cycles: int | None
) -> Blocker | None:
    """Images that sit under no cycle/round dir cannot be assigned to a unit by
    the tree alone: each SBS cycle and phenotype round must arrive in its own dir.
    Loose images are still a single unit when only one can exist (one declared
    cycle; one phenotype round directly under the anchor or a plate dir)."""
    for level in [anchor, *(d for d in _subdirs(anchor) if _match_plate(d.name) is not None)]:
        if any(idx_of(d.name) is not None and _match_plate(d.name) is None for d in _subdirs(level)):
            return None
    images_by_dir: dict[Path, int] = {}
    has_unit_dir = False
    stack = [anchor]
    while stack:
        d = stack.pop()
        for c in d.iterdir():
            if c.name.startswith((PARK_PREFIX, ".")):
                continue
            if c.is_dir():
                if idx_of(c.name) is not None and _match_plate(c.name) is None:
                    has_unit_dir = True
                stack.append(c)
            elif _is_image(c):
                images_by_dir[d] = images_by_dir.get(d, 0) + 1
    if has_unit_dir or not images_by_dir:
        return None
    single_unit_dirs = {anchor} | {d for d in _subdirs(anchor) if _match_plate(d.name)}
    if kind == "cycle" and declared_cycles == 1:
        stray = {d: n for d, n in images_by_dir.items() if d not in single_unit_dirs}
    elif kind == "round":
        stray = {d: n for d, n in images_by_dir.items() if d not in single_unit_dirs}
    else:
        stray = images_by_dir
    if not stray:
        return None
    where = ", ".join(f"{d.relative_to(anchor.parent)} ({n})" for d, n in sorted(stray.items()))
    unit = "c<M>/ per sequencing cycle" if kind == "cycle" else "round_<M>/ per phenotype round"
    canon = "input_sbs" if kind == "cycle" else "input_ph"
    return Blocker(
        str(anchor),
        f"{kind}-unseparated",
        f"image files under {anchor.name} are not separated into {kind} dirs ({where}); "
        f"stage the data as {canon}/plate_<N>/{unit} — the {kind} of a file is not "
        "read from its name",
    )


_MODALITIES = (
    (_is_sbs, "input_sbs", "cycle", _match_cycle, lambda i: f"c{i}", "c1"),
    (_is_ph, "input_ph", "round", _match_round, lambda i: f"round_{i}", "round_1"),
)


def _plan_modality_major(
    root: Path, declared_cycles: int | None, plan: ReshapePlan
) -> None:
    """Tree already has input_sbs/ and/or input_ph/ at root (modality-major)."""
    children = _subdirs(root)
    for is_mod, canon, kind, idx_of, canonical, wrap_name in _MODALITIES:
        group = [d for d in children if is_mod(d.name)]
        if not group:
            plan.blockers.append(
                Blocker(str(root), f"missing-{kind}", f"no {canon}/ at root")
            )
            continue
        if len(group) > 1:
            plan.blockers.append(
                Blocker(
                    str(root),
                    f"{kind}-collision",
                    f"multiple {canon} candidates: " + ", ".join(g.name for g in group),
                )
            )
            continue
        anchor = group[0]
        blocker = unseparated_blocker(anchor, kind, idx_of, declared_cycles)
        if blocker:
            plan.blockers.append(blocker)
            continue
        op = _rename_op(anchor, canon, f"{canon} alias")
        if op:
            plan.ops.append(op)
        counts = _plan_modality_anchor(
            root, anchor, kind, idx_of, canonical, wrap_name, declared_cycles, plan
        )
        if kind == "round":
            _check_round_consistency(anchor, plan)
        if kind == "cycle" and len(set(counts.values())) > 1:
            plan.warnings.append(
                f"non-uniform sequencing-cycle counts across plates: "
                f"{ {f'plate_{k}': v for k, v in sorted(counts.items())} } "
                "— check this is expected"
            )


def _plan_plate_major(
    root: Path, plate_dirs: list[Path], declared_cycles: int | None, plan: ReshapePlan
) -> None:
    """Tree is plate-major (`plate_<N>/input_sbs/...`). Plan the transpose to
    modality-major in a single pass: move each plate's modality subtree to
    `input_<mod>/plate_<N>`. Alias-rename, census and unit fixes happen on the
    next pass once the tree is modality-major."""
    _check_index_contiguity(root, plate_dirs, _match_plate, "plate", plan)
    for plate in plate_dirs:
        idx = _match_plate(plate.name)
        kids = _subdirs(plate)
        for is_mod, canon, kind in (
            (_is_sbs, "input_sbs", "sbs"),
            (_is_ph, "input_ph", "ph"),
        ):
            group = [d for d in kids if is_mod(d.name)]
            if not group:
                plan.blockers.append(
                    Blocker(
                        str(plate), f"missing-{kind}", f"{plate.name} has no {canon} dir"
                    )
                )
                continue
            if len(group) > 1:
                plan.blockers.append(
                    Blocker(
                        str(plate),
                        f"{kind}-collision",
                        f"multiple {canon} candidates in {plate.name}: "
                        + ", ".join(g.name for g in group),
                    )
                )
                continue
            src = group[0]
            unit_kind, idx_of = ("cycle", _match_cycle) if kind == "sbs" else ("round", _match_round)
            blocker = unseparated_blocker(src, unit_kind, idx_of, declared_cycles)
            if blocker:
                plan.blockers.append(blocker)
                continue
            plan.ops.append(
                Op(
                    "move",
                    src=str(src),
                    dst=str(root / canon / f"plate_{idx}"),
                    reason=f"transpose plate-major → modality-major "
                    f"({plate.name}/{src.name})",
                )
            )


def validate_inputs(sbs_dir, ph_dir, declared_cycles: int | None = None) -> ReshapePlan:
    """Check the screen.yaml input dirs against EXPECTED_LAYOUT without reshaping.

    The dirs are the modality anchors whatever they are named. Any op in the returned
    plan means the data is not in the expected layout yet; blockers and warnings are as
    for `plan_reshape`.
    """
    plan = ReshapePlan(root=f"{sbs_dir} | {ph_dir}")
    for d, kind, idx_of, canonical, wrap_name in (
        (sbs_dir, "cycle", _match_cycle, lambda i: f"c{i}", "c1"),
        (ph_dir, "round", _match_round, lambda i: f"round_{i}", "round_1"),
    ):
        if not d:
            continue
        anchor = Path(d)
        if not anchor.is_dir():
            plan.blockers.append(Blocker(str(anchor), f"missing-{kind}", f"{anchor} is not a directory"))
            continue
        blocker = unseparated_blocker(anchor, kind, idx_of, declared_cycles)
        if blocker:
            plan.blockers.append(blocker)
            continue
        _plan_modality_anchor(
            anchor.parent, anchor, kind, idx_of, canonical, wrap_name, declared_cycles, plan
        )
        if kind == "round":
            _check_round_consistency(anchor, plan)
    return plan


def plan_reshape(root, declared_cycles: int | None = None) -> ReshapePlan:
    """Walk `root` (the screen data dir) and return the reshape plan needed to
    reach the canonical modality-major OPS hierarchy. Pure — never mutates."""
    root = Path(root)
    plan = ReshapePlan(root=str(root))
    if not root.is_dir():
        plan.blockers.append(Blocker(str(root), "no-root", f"{root} is not a directory"))
        return plan

    children = _subdirs(root)
    modality = [d for d in children if _is_sbs(d.name) or _is_ph(d.name)]
    plate_all = [d for d in children if _match_plate(d.name) is not None]
    plate_dirs = [d for d in plate_all if not _dir_empty(d)]
    empty_plates = [d for d in plate_all if _dir_empty(d)]

    # Prune plate dirs emptied by a transpose (so they don't read as a second,
    # conflicting organization on the next pass).
    for d in empty_plates:
        plan.ops.append(Op("prune", src=str(d), dst="", reason="empty plate dir"))

    if modality and not plate_dirs:
        _plan_modality_major(root, declared_cycles, plan)
    elif plate_dirs and not modality:
        _plan_plate_major(root, plate_dirs, declared_cycles, plan)
    elif modality and plate_dirs:
        plan.blockers.append(
            Blocker(
                str(root),
                "ambiguous-root",
                "both plate_*/ and input_sbs|input_ph/ present at root — "
                "cannot tell plate-major from modality-major",
            )
        )
    elif not empty_plates:
        plan.blockers.append(
            Blocker(
                str(root),
                "no-data",
                f"{root.name} has no plate_*/ or input_sbs/input_ph/",
            )
        )
    return plan


# ---------------------------------------------------------------------------
# Apply — iterate to a fixpoint so nested renames/moves/wraps land safely.
# Each pass re-plans against the live tree (paths never go stale).
# ---------------------------------------------------------------------------


def _apply_one(op: Op) -> None:
    src = Path(op.src)
    if op.op == "prune":
        if src.is_dir() and _dir_empty(src):
            src.rmdir()
        return
    dst = Path(op.dst)
    if op.op == "wrap":
        dst.mkdir(parents=True, exist_ok=True)
        for child in list(src.iterdir()):
            if child == dst or child.name.startswith(PARK_PREFIX):
                continue
            shutil.move(str(child), str(dst / child.name))
        return
    # rename | move | park — relocate src → dst.
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        if src.is_dir() and dst.is_dir():
            # Merge into an existing destination (e.g. re-pull into a parked dir).
            for child in list(src.iterdir()):
                shutil.move(str(child), str(dst / child.name))
            if _dir_empty(src):
                src.rmdir()
        # If dst exists and isn't a dir-merge, leave src in place (planner will
        # flag the collision on the next pass) rather than clobber data.
        return
    shutil.move(str(src), str(dst))


_OP_ORDER = {"park": 0, "wrap": 1, "move": 2, "rename": 3}


def _apply_sort_key(o: Op):
    if o.op == "prune":
        return (1, 0, 0)  # prune emptied dirs last
    return (0, -len(Path(o.src).parts), _OP_ORDER.get(o.op, 9))


def apply_plan(root, declared_cycles: int | None = None) -> dict:
    """Reshape `root` in place to canonical. Refuses if any blocker is present.
    Returns {applied, blocked, passes, warnings}."""
    first = plan_reshape(root, declared_cycles)
    if first.blockers:
        return {"applied": False, "blocked": True, "passes": 0, "warnings": first.warnings}
    if first.is_canonical:
        return {"applied": False, "blocked": False, "passes": 0, "warnings": first.warnings}

    passes = 0
    while passes < MAX_PASSES:
        plan = plan_reshape(root, declared_cycles)
        if plan.blockers:
            return {"applied": True, "blocked": True, "passes": passes, "warnings": plan.warnings}
        if plan.is_canonical:
            return {"applied": True, "blocked": False, "passes": passes, "warnings": plan.warnings}
        for op in sorted(plan.ops, key=_apply_sort_key):
            _apply_one(op)
        passes += 1
    return {"applied": True, "blocked": False, "passes": passes, "warnings": []}


# ---------------------------------------------------------------------------
# Census — positive facts about the tree (for additional_notes reconciliation).
# Tolerant of either layout (modality-major or raw plate-major).
# ---------------------------------------------------------------------------


def census_tree(root, declared_cycles: int | None = None) -> dict:
    """Walk `root` and return per-plate cycle/round counts + anomalies. Works
    on either layout. Pure/read-only. Shape:

        {plates, cycles_per_plate, rounds_per_plate, max_cycles,
         uniform_cycles, declared_cycles, surplus_cycles, junk_dirs}
    """
    root = Path(root)
    cycles_per_plate: dict[int, int] = {}
    rounds_per_plate: dict[int, int] = {}
    junk_dirs: list[str] = []

    def _count_units(plate_dir: Path, idx_of) -> int:
        units, junk = 0, []
        for d in _subdirs(plate_dir):
            if idx_of(d.name) is not None:
                units += 1
            else:
                junk.append(d)
        for d in junk:
            try:
                junk_dirs.append(str(d.relative_to(root)))
            except ValueError:
                junk_dirs.append(d.name)
        return units

    if not root.is_dir():
        return {
            "plates": [], "cycles_per_plate": {}, "rounds_per_plate": {},
            "max_cycles": 0, "uniform_cycles": True, "declared_cycles": declared_cycles,
            "surplus_cycles": {}, "junk_dirs": [],
        }

    children = _subdirs(root)
    modality_major = any(_is_sbs(d.name) or _is_ph(d.name) for d in children)

    if modality_major:
        sbs = next((d for d in children if _is_sbs(d.name)), None)
        ph = next((d for d in children if _is_ph(d.name)), None)
        if sbs:
            for p in _subdirs(sbs):
                i = _match_plate(p.name)
                if i is not None:
                    cycles_per_plate[i] = _count_units(p, _match_cycle)
        if ph:
            for p in _subdirs(ph):
                i = _match_plate(p.name)
                if i is not None:
                    rounds_per_plate[i] = _count_units(p, _match_round)
    else:
        for p in children:
            i = _match_plate(p.name)
            if i is None:
                continue
            for d in _subdirs(p):
                if _is_sbs(d.name):
                    cycles_per_plate[i] = _count_units(d, _match_cycle)
                elif _is_ph(d.name):
                    rounds_per_plate[i] = _count_units(d, _match_round)

    plates = sorted(set(cycles_per_plate) | set(rounds_per_plate))
    counts = list(cycles_per_plate.values())
    max_cycles = max(counts) if counts else 0
    surplus = {}
    if declared_cycles:
        surplus = {p: n - declared_cycles for p, n in cycles_per_plate.items() if n > declared_cycles}
    return {
        "plates": plates,
        "cycles_per_plate": cycles_per_plate,
        "rounds_per_plate": rounds_per_plate,
        "max_cycles": max_cycles,
        "uniform_cycles": len(set(counts)) <= 1,
        "declared_cycles": declared_cycles,
        "surplus_cycles": surplus,
        "junk_dirs": junk_dirs,
    }


# ---------------------------------------------------------------------------
# rsync filter — keep a re-pull from re-introducing parked / reshaped data.
# ---------------------------------------------------------------------------


def rsync_filter_lines() -> list[str]:
    """Filter rules (rsync `--filter` / gcloud `--exclude` style) that protect
    the on-VM reshape from a naive re-pull of the still-plate-major bucket."""
    return [
        f"- /{EXCLUDED_CYCLES_DIR}/",
        f"- /{NON_IMAGE_DIR}/",
        "- /_config/",
        "- /_reshape_manifests/",
        "- /_*/",
    ]


# ---------------------------------------------------------------------------
# screen.yaml → declared cycle count
# ---------------------------------------------------------------------------


def declared_cycles_from_screen_yaml(path) -> int | None:
    """Read iss.cycles from a screen.yaml (best-effort; None if unavailable)."""
    try:
        import yaml  # local import: only needed for --screen-yaml
    except Exception:
        return None
    try:
        with open(path) as fh:
            doc = yaml.safe_load(fh) or {}
    except Exception:
        return None
    iss = doc.get("iss") if isinstance(doc, dict) else None
    if isinstance(iss, dict):
        c = iss.get("cycles")
        if isinstance(c, int):
            return c
    return None


def validate_screen_inputs(screen_yaml) -> ReshapePlan:
    """`validate_inputs` on the data.input_sbs_dir / data.input_phenotype_dir a screen.yaml
    names (relative paths resolve against its directory), with its iss.cycles."""
    import yaml

    path = Path(screen_yaml)
    doc = yaml.safe_load(path.read_text()) or {}
    data = doc.get("data") or {}

    def _resolve(d):
        if not d:
            return None
        d = Path(d)
        return d if d.is_absolute() else path.parent / d

    plan = validate_inputs(
        _resolve(data.get("input_sbs_dir")),
        _resolve(data.get("input_phenotype_dir")),
        declared_cycles_from_screen_yaml(path),
    )
    if not data.get("input_sbs_dir") and not data.get("input_phenotype_dir"):
        plan.blockers.append(
            Blocker(str(path), "no-input-dirs", "screen.yaml names no data.input_sbs_dir or input_phenotype_dir")
        )
    return plan


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_plan(plan: ReshapePlan) -> None:
    if plan.is_canonical and not plan.warnings:
        print(f"✓ {plan.root} is canonical — no reshape needed.")
        return
    if plan.is_canonical:
        print(f"✓ {plan.root} is canonical (with warnings below).")
    if plan.ops:
        print(f"Reshape plan for {plan.root}:")
        for o in plan.ops:
            if o.op == "rename":
                print(f"  rename  {Path(o.src).name} → {Path(o.dst).name}    ({o.reason})")
            elif o.op == "prune":
                print(f"  prune   {o.src}    ({o.reason})")
            else:
                print(f"  {o.op:<6}  {o.src} → {o.dst}    ({o.reason})")
    if plan.blockers:
        print("\nBLOCKERS (not auto-reshaped; the agent resolves each with the operator, see brieflow-setup \"Raw data staging\"):")
        for b in plan.blockers:
            print(f"  [{b.code}] {b.path}: {b.message}")
    if plan.warnings:
        print("\nCONFIRM WITH THE OPERATOR before running (exit 3):")
        for w in plan.warnings:
            print(f"  ⚠ {w}")
    if plan.blockers or plan.ops or plan.warnings:
        print("\nRun with --layout for the expected structure.")


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate + reshape an OPS raw-data tree.")
    ap.add_argument("--root", help="Screen data dir (e.g. /data/<screen>)")
    ap.add_argument("--layout", action="store_true", help="Print the expected raw-data layout and exit")
    ap.add_argument(
        "--inputs-from", default=None,
        help="Check the input dirs this screen.yaml names (read-only; no reshape)",
    )
    ap.add_argument("--apply", action="store_true", help="Execute the reshape plan in place")
    ap.add_argument("--json", action="store_true", help="Emit the plan as JSON")
    ap.add_argument("--cycles", type=int, default=None, help="Declared iss.cycles (park surplus above this)")
    ap.add_argument("--screen-yaml", default=None, help="Read iss.cycles from this screen.yaml")
    ap.add_argument("--rsync-filter", default=None, help="Write an rsync/gcloud filter file and exit")
    args = ap.parse_args()

    if args.layout:
        print(EXPECTED_LAYOUT)
        return 0
    if args.inputs_from:
        plan = validate_screen_inputs(args.inputs_from)
        if args.json:
            json.dump(plan.to_dict(), sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            _print_plan(plan)
        return 1 if plan.blockers else 2 if plan.ops else 3 if plan.warnings else 0
    if not args.root:
        ap.error("--root is required (or pass --layout / --inputs-from)")

    if args.rsync_filter:
        Path(args.rsync_filter).write_text("\n".join(rsync_filter_lines()) + "\n")
        if not args.json:
            print(f"Wrote rsync filter → {args.rsync_filter}")
        return 0

    declared = args.cycles
    if declared is None and args.screen_yaml:
        declared = declared_cycles_from_screen_yaml(args.screen_yaml)

    plan = plan_reshape(args.root, declared)

    if args.apply:
        if plan.blockers:
            if args.json:
                json.dump({"applied": False, "blocked": True, **plan.to_dict()}, sys.stdout, indent=2)
                sys.stdout.write("\n")
            else:
                _print_plan(plan)
                print("\nRefusing to reshape while blockers are present.")
            return 1
        res = apply_plan(args.root, declared)
        after = plan_reshape(args.root, declared)
        if args.json:
            json.dump({**res, "after": after.to_dict()}, sys.stdout, indent=2)
            sys.stdout.write("\n")
        else:
            print(f"Applied reshape ({res['passes']} pass(es)).")
            _print_plan(after)
        return 1 if after.blockers or not after.is_canonical else 0

    if args.json:
        json.dump(plan.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        _print_plan(plan)

    if plan.blockers:
        return 1
    if plan.ops:
        return 2
    return 3 if plan.warnings else 0


if __name__ == "__main__":
    sys.exit(main())
