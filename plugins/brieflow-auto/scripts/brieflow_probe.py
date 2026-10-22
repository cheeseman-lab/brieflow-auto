#!/usr/bin/env python3
"""brieflow_probe.py — data-inspection probes.

Auto-detect data format, organization, z-planes, round order, and filename
path patterns by inspecting staged raw data. Pure stdlib + lazy nd2/glob; no
dependency on other plugin modules. Re-exported from brieflow_wizard.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

__all__ = [
    "detect_data_format",
    "detect_organization",
    "detect_z_planes",
    "detect_round_order",
    "infer_path_pattern",
    "infer_split_tiff_layout",
    "probe_preprocess",
    "probe_det_range",
]


#: A single format must hold at least this share of recognized image files for
#: the dir to count as that format rather than "mixed". Lets a handful of stray
#: overview/QC images (plate snapshots, screenshots) coexist with thousands of
#: real nd2 tiles without flipping a uniform dataset to "mixed".
_FORMAT_DOMINANCE = 0.95


def detect_data_format(images_dir: str | Path) -> str:
    """Scan entries for image-format signature.
    Returns "nd2" | "ims" | "tiff" | "zarr" | "mixed" | "unknown".

    When more than one format is present, returns the dominant one if it holds
    ≥ _FORMAT_DOMINANCE of recognized image files (stray non-pipeline images are
    ignored); only a genuine split returns "mixed"."""
    p = Path(images_dir)
    if not p.exists():
        return "unknown"
    counts: dict[str, int] = {"nd2": 0, "ims": 0, "tiff": 0, "zarr": 0}
    for child in p.rglob("*"):
        if child.is_dir() and child.suffix == ".zarr":
            counts["zarr"] += 1
            continue
        if not child.is_file():
            continue
        if child.suffix == ".nd2":
            counts["nd2"] += 1
        elif child.suffix == ".ims":
            counts["ims"] += 1
        elif child.suffix in (".tif", ".tiff"):
            counts["tiff"] += 1
    present = {k: v for k, v in counts.items() if v}
    total = sum(present.values())
    if total == 0:
        return "unknown"
    if len(present) == 1:
        return next(iter(present))
    top = max(present, key=present.get)
    return top if present[top] / total >= _FORMAT_DOMINANCE else "mixed"


def detect_organization(
    images_dir: str | Path,
    path_pattern: str | None = None,
    well_group_index: int = 0,
    data_format: str | None = None,
) -> str:
    """Determine well-vs-tile organization.

    For nd2: read sizes['P'] from any sample file — P>1 means multi-FOV per
    file ("well" organization, the Cheeseman default), P==1 means one FOV per
    file ("tile" organization). This is more reliable than counting files
    because split-cycle SBS has many files per well even when each one is
    multi-FOV.

    For tiff: fall back to file-count heuristic on the regex's well capture
    group — if avg files-per-well ≤ 1.5 → "well", else "tile".

    Returns "well" | "tile" | "unknown".
    """
    p = Path(images_dir)
    if not p.exists():
        return "unknown"

    # Imaris files hold one FOV each, so the layout is always tile-organized
    if data_format == "ims" or (data_format is None and any(p.rglob("*.ims"))):
        return "tile"

    if data_format == "nd2" or (data_format is None and any(p.rglob("*.nd2"))):
        try:
            import nd2

            sample = next(p.rglob("*.nd2"), None)
            if sample is not None:
                with nd2.ND2File(str(sample)) as nd:
                    s = nd.sizes
                    if "P" in s and s["P"] > 1:
                        return "well"
                    if "P" in s and s["P"] == 1:
                        return "tile"
        except ImportError:
            pass  # nd2 not installed; fall through
        except Exception:
            pass  # fall through to file-count heuristic

    if path_pattern is None:
        return "unknown"
    import glob

    rx = re.compile(path_pattern)
    counts: dict[str, int] = {}
    for f in glob.glob(str(p / "**" / "*"), recursive=True):
        rel = str(Path(f).relative_to(p))
        m = rx.search(rel)
        if not m or m.lastindex is None or m.lastindex <= well_group_index:
            continue
        well = m.group(well_group_index + 1)
        counts[well] = counts.get(well, 0) + 1
    if not counts:
        return "unknown"
    avg = sum(counts.values()) / len(counts)
    return "well" if avg <= 1.5 else "tile"


def detect_z_planes(
    images_dir: str | Path,
    path_pattern: str,
    data_format: str,
    tuple_group_indices: tuple[int, ...] = (0, 1, 2, 3),
    z_group_index: int | None = None,
) -> int | None:
    """Z-plane count detection. Returns None for nd2/zarr (z is internal to
    the container).

    Two tiff modes:
      - `z_group_index=None` (default, unchanged): groups files by the
        `tuple_group_indices` path-metadata tuple and returns the file count
        per tuple if uniform (>1), else None. Assumes one file per z-plane
        per FOV tuple.
      - `z_group_index` set: split-tiff filenames carry the z-plane as its
        own regex capture (e.g. Opera Phenix `pXX`) rather than one-file-
        per-z: counts the DISTINCT values captured at that group index
        across all matching files and returns the count if >1, else None.

    `tuple_group_indices` / `z_group_index` are 0-based against the regex's
    capture groups (`m.group(i + 1)`)."""
    if data_format in ("nd2", "ims", "zarr"):
        return None
    import glob

    p = Path(images_dir)
    rx = re.compile(path_pattern)
    files = glob.glob(str(p / "**" / "*.tif*"), recursive=True)

    if z_group_index is not None:
        z_values: set[str] = set()
        for f in files:
            rel = str(Path(f).relative_to(p))
            m = rx.search(rel)
            if not m:
                continue
            try:
                z_values.add(m.group(z_group_index + 1))
            except IndexError:
                continue
        return len(z_values) if len(z_values) > 1 else None

    by_tuple: dict[tuple, int] = {}
    for f in files:
        rel = str(Path(f).relative_to(p))
        m = rx.search(rel)
        if not m:
            continue
        try:
            key = tuple(m.group(i + 1) for i in tuple_group_indices)
        except IndexError:
            continue
        by_tuple[key] = by_tuple.get(key, 0) + 1
    if not by_tuple:
        return None
    counts = set(by_tuple.values())
    if counts == {1}:
        return None
    if len(counts) == 1:
        return counts.pop()
    return None  # non-uniform — caller should ask


def detect_round_order(
    images_dir: str | Path,
    path_pattern: str | None = None,
) -> list[int]:
    """Scan for `round_N` subdirectories under images_dir. Returns sorted
    list of round integers, or [1] if no round_N pattern found (single-round
    phenotype is the default Cheeseman convention)."""
    p = Path(images_dir)
    if not p.exists():
        return [1]
    rounds: set[int] = set()
    rd_re = re.compile(r"round[_-]?(\d+)", re.IGNORECASE)
    for d in p.rglob("*"):
        if not d.is_dir():
            continue
        m = rd_re.search(d.name)
        if m:
            rounds.add(int(m.group(1)))
    return sorted(rounds) if rounds else [1]


# Dir-token regexes shared between infer_path_pattern (named tokens) and
# infer_split_tiff_layout (generic split-tiff). Cycle allows an optional
# NIS-Elements-style `_YYYYMMDD_HHMMSS_NNN` timestamp suffix between the
# cycle digits and the trailing `/` — the tight pattern `c(\d+)/` missed
# `c10_20251110_122928_380/`-style dirs.
_PLATE_DIR_RE = r"plate[_-]?(\d+)"
_CYCLE_DIR_RE = r"c(\d+)(?:_\d+_\d+_\d+)?/"
_ROUND_DIR_RE = r"round[_-]?(\d+)"


_NUMERIC_WELL_RE = re.compile(r"Wells?[-_]?(\d+)(?=[_\-.])")


def has_cycle_dirs(images_dir: str | Path, declared_cycles: int | None) -> bool:
    """True when SBS images sit in per-cycle dirs (c<M>/, cycle_<M>/), or when fewer
    than two cycles are declared (nothing to separate, or iss.cycles unknown)."""
    if not isinstance(declared_cycles, int) or declared_cycles < 2:
        return True
    cycle_re = re.compile(r"c(?:ycle)?[_-]?\d+(?:_\d+_\d+_\d+)?", re.IGNORECASE)
    for _, dirs, _ in os.walk(images_dir):
        dirs[:] = [d for d in dirs if not d.startswith(("_", "."))]
        if any(cycle_re.fullmatch(d) for d in dirs):
            return True
    return False


def numeric_well_tokens(sample_filenames: Iterable[str]) -> list[str]:
    """Distinct numeric well tokens (`Well1_Point1_...`) when most samples carry one.

    brieflow wells are row+column (`A1`); a bare well number can't be mapped without the
    operator, and the pattern search otherwise reads the next number (the point) as the tile."""
    samples = list(sample_filenames)
    found = [m.group(1) for s in samples if (m := _NUMERIC_WELL_RE.search(Path(s).name))]
    if not samples or len(found) < 0.8 * len(samples):
        return []
    return sorted(set(found), key=int)


def infer_path_pattern(sample_filenames: Iterable[str]) -> list[dict]:
    """Propose candidate regex patterns for a list of sample filenames.

    Walks well-known token shapes (Well[A-Z]\\d+, c\\d+, round_\\d+, plate_\\d+,
    Channel...) and constructs candidate regexes that capture each. Returns
    a ranked list of:

        [{"regex": ..., "captures": [...], "match_rate": float,
          "distinct_values": {<capture_name>: [token, token, ...], ...}}]

    The `distinct_values` summary is the ground-truth output of the regex
    against the actual files — wizard uses it directly for token-keyed lookups
    (e.g., `SBS_CHANNEL_ORDER`). Never substitute biological labels from
    screen.yaml or fluorophore tokens from ND2 metadata for these values.

    The channel capture is **greedy** by default: it matches the FULL token
    between `Channel` and the next `_Seq` or `.nd2`/`.tiff`, so composite
    multichannel filenames like
    `WellA1_ChannelDapi_1p-CY3_30p_545-A594_30p-CY5_30p-CY7_30p_Seq0000.nd2`
    capture the whole `Dapi_1p-CY3_30p_545-A594_30p-CY5_30p-CY7_30p` token,
    not just `Dapi`. Operator then sees the actual filename token, not a
    silently-truncated stub.
    """
    samples = list(sample_filenames)
    if not samples:
        return []

    token_patterns = [
        (_PLATE_DIR_RE, "plate"),
        (_CYCLE_DIR_RE, "cycle"),
        (_ROUND_DIR_RE, "round"),
        # Well: allow `Well A1`, `WellA1`, AND `Wells-A1` (Nikon NIS-Elements
        # uses the `Wells-` prefix). Original tight pattern `Well([A-Z]\d+)`
        # missed `Wells-A1` because `Wells` doesn't match `Well` followed by
        # an uppercase letter — the `s` lowercases.
        (r"Wells?-?([A-Z]\d+)", "well"),
        # Greedy channel match: captures everything between `Channel` and the next
        # `_Seq` / `.nd2` / `.tiff` / end-of-string. Operator gets the FULL filename
        # token (composite or single-channel), never a lazy-truncated stub.
        (r"Channel([A-Za-z0-9_+\-]+?)(?=_Seq|\.nd2|\.tif|\.tiff|$)", "channel"),
        (r"_Seq(\d+)", "seq"),
        (r"Points?-?(\d+)", "tile"),
    ]

    candidates: list[dict] = []
    # One candidate per ordered subset of tokens that all appear in at least 80% of samples
    for n in range(2, len(token_patterns) + 1):
        from itertools import combinations

        for combo in combinations(token_patterns, n):
            pieces, captures = [], []
            for tok_re, name in combo:
                pieces.append(tok_re)
                captures.append(name)
            joined = r".*".join(pieces) + r".*"
            try:
                rx = re.compile(joined)
            except re.error:
                continue
            distinct: dict[str, list[str]] = {c: [] for c in captures}
            seen_per_cap: dict[str, set[str]] = {c: set() for c in captures}
            hits = 0
            for s in samples:
                m = rx.search(s)
                if not m:
                    continue
                hits += 1
                for i, cap_name in enumerate(captures, start=1):
                    val = m.group(i)
                    if val not in seen_per_cap[cap_name]:
                        seen_per_cap[cap_name].add(val)
                        distinct[cap_name].append(val)
            rate = hits / len(samples)
            if rate >= 0.8:
                candidates.append(
                    {
                        "regex": joined,
                        "captures": captures,
                        "match_rate": rate,
                        "n_tokens": n,
                        "distinct_values": distinct,
                    }
                )
    # Prefer more captures + higher match rate
    candidates.sort(key=lambda c: (c["match_rate"], c["n_tokens"]), reverse=True)
    # Dedup by capture-tuple (keep highest-scored)
    seen, dedup = set(), []
    for c in candidates:
        k = tuple(c["captures"])
        if k in seen:
            continue
        seen.add(k)
        dedup.append(c)
    return dedup[:5]


# ---------------------------------------------------------------------------
# Comprehensive preprocess probe — "see what the data looks like" and prefill.
# ---------------------------------------------------------------------------

# Per-side raw-image extensions, most-specific first.
_RAW_EXTS = (".nd2", ".tiff", ".tif")

# Directory names that hold superseded re-imaging / scratch, not primary data.
# Files under these are excluded from sampling: they pollute path-pattern + channel
# inference (e.g. an `Archive/C6_oldB1_.../` dir whose name doesn't match the `c<M>/`
# cycle pattern makes the inferrer drop the cycle group → samples df has no cycle →
# crash). OUTSTANDING #3 (matterhorn + kazbek both hit this).
_JUNK_DIR_RE = re.compile(r"^(archive|backup|old|orig|originals?|tmp|temp|scratch|trash|junk|_.*|\..*)$", re.IGNORECASE)


def _is_junk_path(rel_parts: Iterable[str]) -> bool:
    """True if any directory component looks like a non-data (archive/scratch) dir."""
    return any(_JUNK_DIR_RE.match(part) for part in rel_parts)


def _sample_files(images_dir: str | Path, n: int = 3) -> list[Path]:
    """Return up to `n` raw image files under `images_dir` (recursive), sorted
    for determinism. Empty list if the dir is missing or has none. Files under
    junk dirs (archive/backup/scratch/hidden) are skipped — see _JUNK_DIR_RE."""
    p = Path(images_dir)
    if not p.exists():
        return []
    hits: list[Path] = []
    for ext in _RAW_EXTS:
        hits = sorted(
            f for f in p.rglob(f"*{ext}")
            if f.is_file() and not _is_junk_path(f.relative_to(p).parts[:-1])
        )
        if hits:
            break
    return hits[:n]


def _tokenize_filename(name: str) -> list[dict]:
    """Split a basename into maximal (letters, digits) runs for role inference.

    Strips the extension first so it isn't tokenized (e.g. the `1` in `.tiff` is
    not a token). Returns one dict per run in order:
    `{"prefix": str, "value": str, "start": int, "end": int}`. `prefix` is
    lowercased for role matching; `value` keeps the raw digit string (leading
    zeros preserved) since it's an opaque token, not a number to be parsed.
    """
    stem = Path(name).stem
    return [
        {"prefix": m.group(1).lower(), "value": m.group(2), "start": m.start(), "end": m.end()}
        for m in re.finditer(r"([A-Za-z]+)(\d+)", stem)
    ]


# Prefix-hint vocab — PRIMARY signal for role assignment (module constants,
# extensible). A prefix that matches one of these gets that role outright,
# whether or not it happens to vary across the sampled basenames (a
# single-row sample still has a `r` well token; it just doesn't vary yet).
# Cardinality is only a tiebreak for prefixes NONE of these cover.
WELL_HINTS = {"r", "c", "w", "well", "row", "col"}
TILE_HINTS = {"f", "field", "tile", "point", "points", "site", "fov"}
Z_HINTS = {"p", "z", "plane"}
CHANNEL_HINTS = {"ch", "channel", "chan"}


def _classify_filename_tokens(
    basenames: list[str], screen_channel_count: int | None
) -> dict | None:
    """Classify tokenized filename prefixes into well/tile/channel/z roles.

    PREFIX-HINT-PRIMARY: tokenizes every basename via `_tokenize_filename`,
    keeps only prefixes common to ALL of them, then assigns each one a role
    by looking it up in the `WELL_HINTS` / `TILE_HINTS` / `CHANNEL_HINTS` /
    `Z_HINTS` vocab. A hinted prefix gets its role unconditionally — this is
    what makes real multi-well data parse: on Opera Phenix filenames `r`/`c`
    (well) out-number `p`/`ch` (z/channel) in distinct values, so a
    cardinality-first scheme mis-assigns row/col as the sub-FOV pool. Hints
    win regardless.

    Prefixes with NO hint match are the "unknown" pool. A constant unknown
    (1 distinct value — e.g. Opera Phenix's `sk1fk1fl1` skip/flim/flatfield
    markers) carries no signal and is dropped. A varying unknown is a
    channel/z candidate, disambiguated by cardinality:
      - the candidate whose distinct count == screen_channel_count is
        channel, if that resolves exactly one (and no channel hint already
        claimed the role);
      - the one candidate left after that is z, if exactly one remains;
      - anything still unresolved is reported in `ambiguous` rather than
        guessed — a later step routes it to the operator.

    Returns `None` if no tile_prefix or no well_prefixes were found (no
    coherent well+tile structure to classify — caller falls back to named
    tokens), else::

        {"well_prefixes": [str, ...], "tile_prefix": str,
         "channel_prefix": str | None, "z_prefix": str | None,
         "ambiguous": [str, ...]}
    """
    if not basenames:
        return None

    per_name_tokens = [_tokenize_filename(name) for name in basenames]
    prefix_sets = [{t["prefix"] for t in toks} for toks in per_name_tokens]
    common = set.intersection(*prefix_sets)
    if not common:
        return None

    # First-occurrence order (from the first basename) — common prefixes are
    # in every basename, so this covers all of them and gives a deterministic
    # ordering for both output lists (e.g. well_prefixes in filename order)
    # and the unknown-pool cardinality tiebreak.
    order: list[str] = []
    seen: set[str] = set()
    for t in per_name_tokens[0]:
        if t["prefix"] in common and t["prefix"] not in seen:
            order.append(t["prefix"])
            seen.add(t["prefix"])

    values: dict[str, set[str]] = {prefix: set() for prefix in common}
    for toks in per_name_tokens:
        for t in toks:
            values[t["prefix"]].add(t["value"])
    cardinality = {prefix: len(vals) for prefix, vals in values.items()}

    well_prefixes: list[str] = []
    tile_prefix: str | None = None
    channel_prefix: str | None = None
    z_prefix: str | None = None
    unknown: list[str] = []

    for prefix in order:
        if prefix in WELL_HINTS:
            well_prefixes.append(prefix)
        elif prefix in TILE_HINTS:
            if tile_prefix is None:
                tile_prefix = prefix
        elif prefix in CHANNEL_HINTS:
            if channel_prefix is None:
                channel_prefix = prefix
        elif prefix in Z_HINTS:
            if z_prefix is None:
                z_prefix = prefix
        elif cardinality[prefix] > 1:
            unknown.append(prefix)
        # else: constant, unhinted — dropped (e.g. sk/fk/fl)

    ambiguous: list[str] = []
    if unknown:
        count_matches = [
            prefix
            for prefix in unknown
            if screen_channel_count is not None and cardinality[prefix] == screen_channel_count
        ]
        if channel_prefix is None and len(count_matches) == 1:
            channel_prefix = count_matches[0]

        remaining = [prefix for prefix in unknown if prefix != channel_prefix]
        if z_prefix is None and len(remaining) == 1:
            z_prefix = remaining[0]
            remaining = []
        ambiguous = [prefix for prefix in remaining if prefix != z_prefix]

    if tile_prefix is None or not well_prefixes:
        return None

    return {
        "well_prefixes": well_prefixes,
        "tile_prefix": tile_prefix,
        "channel_prefix": channel_prefix,
        "z_prefix": z_prefix,
        "ambiguous": ambiguous,
    }


def _natural_sort_tokens(tokens: Iterable[str]) -> list[str]:
    """Sort `<prefix><digits>` tokens numerically on the digit run so
    `ch1 < ch2 < ch10` (plain lexicographic sort would put `ch10` before
    `ch2`)."""
    return sorted(
        tokens,
        key=lambda t: (re.sub(r"\d+", "", t), int(re.search(r"\d+", t).group())),
    )


def infer_split_tiff_layout(
    rel_paths: list[str], side: str, screen_channel_count: int | None
) -> dict | None:
    """Build a generic split-tiff regex + path_metadata from filename structure
    alone (Opera Phenix and similar per-channel/per-z-plane tiff conventions,
    where the pipeline's named tokens — `Well`/`Channel`/`Points` — don't
    appear). Classifies the basenames via `_classify_filename_tokens`; if that
    can't find a coherent well+tile structure, returns `None` (caller falls
    back to `infer_path_pattern`).

    Regex groups are built left-to-right: `plate_(\\d+)` … the side's cycle
    (`c(\\d+)/`) or round (`round[_-]?(\\d+)`) dir token, whichever is present
    in the sample path … then filename groups in the CANONICAL order
    well → tile → channel → z (regardless of which of channel/z physically
    trails the other in the filename — Opera Phenix has z (`p`) before
    channel (`ch`) — so channel is captured via a non-consuming lookahead
    when both are present, keeping its group index ahead of z's while still
    matching correctly positioned text).

    `well` is a single compound capture group over the concatenated
    well-prefix tokens (e.g. `(r\\d+c\\d+)`); `channel` captures the full
    token including its prefix (e.g. `ch1`) since that's the literal value
    used downstream (channel_order, samples_df); `tile`/`z` capture digits
    only.

    Returns::

        {"path_pattern": str, "path_metadata": [str, ...],
         "metadata_order_type": {str: str}, "channel_order": [str,...]|None,
         "n_z_planes": int|None}

    or `None`. `path_metadata` lines up positionally with the regex's capture
    groups — callers (create_samples_df) zip them, so order must match.
    """
    if not rel_paths:
        return None

    basenames = [rp.rsplit("/", 1)[-1] for rp in rel_paths]
    cls = _classify_filename_tokens(basenames, screen_channel_count)
    if cls is None:
        return None

    probe_path = rel_paths[0]
    pieces: list[str] = []
    path_metadata: list[str] = []

    if re.search(_PLATE_DIR_RE, probe_path):
        pieces.append(_PLATE_DIR_RE)
        path_metadata.append("plate")

    cycle_round_re = _CYCLE_DIR_RE if side == "sbs" else _ROUND_DIR_RE
    cycle_round_name = "cycle" if side == "sbs" else "round"
    if re.search(cycle_round_re, probe_path):
        pieces.append(cycle_round_re)
        path_metadata.append(cycle_round_name)

    well_piece = "(" + "".join(rf"{p}\d+" for p in cls["well_prefixes"]) + ")"
    pieces.append(well_piece)
    path_metadata.append("well")

    pieces.append(rf"{cls['tile_prefix']}(\d+)")
    path_metadata.append("tile")

    channel_prefix = cls["channel_prefix"]
    z_prefix = cls["z_prefix"]
    if channel_prefix and z_prefix:
        pieces.append(rf"(?=.*?({channel_prefix}\d+))")
        path_metadata.append("channel")
        pieces.append(rf".*?{z_prefix}(\d+)")
        path_metadata.append("z")
    elif channel_prefix:
        pieces.append(rf"({channel_prefix}\d+)")
        path_metadata.append("channel")
    elif z_prefix:
        pieces.append(rf"{z_prefix}(\d+)")
        path_metadata.append("z")

    path_pattern = r".*".join(pieces) + r".*"
    metadata_order_type = {
        col: (str.__name__ if col in ("well", "channel") else int.__name__)
        for col in path_metadata
    }

    channel_order = None
    if channel_prefix:
        tokens = {
            m.group(0) for name in basenames if (m := re.search(rf"{channel_prefix}\d+", name))
        }
        channel_order = _natural_sort_tokens(tokens)

    n_z_planes = None
    if z_prefix:
        z_values = {
            m.group(1) for name in basenames if (m := re.search(rf"{z_prefix}(\d+)", name))
        }
        n_z_planes = len(z_values) or None

    return {
        "path_pattern": path_pattern,
        "path_metadata": path_metadata,
        "metadata_order_type": metadata_order_type,
        "channel_order": channel_order,
        "n_z_planes": n_z_planes,
    }


def _index_xml_metadata_layout(images_dir: str | Path, side: str) -> dict | None:
    """External-metadata (`*_METADATA_*`) vars for an Opera Phenix tiff side.

    Opera Phenix keeps per-field stage positions in an `Index.xml` inside the image
    tree (not a per-tile CSV). Point `create_samples_df` at those files: capture the
    same plate/cycle dir tokens the image pattern uses, so the inventory is one coarse
    row per `Index.xml` (per-FOV narrowing happens in `extract_metadata_tiff`). Returns
    bare probe fields, or `None` when the tree holds no `Index.xml`.
    """
    import glob

    hits = glob.glob(str(Path(images_dir) / "**" / "Index.xml"), recursive=True)
    if not hits:
        return None
    rel = str(Path(hits[0]).relative_to(Path(images_dir)))
    pieces: list[str] = []
    meta: list[str] = []
    if re.search(_PLATE_DIR_RE, rel):
        pieces.append(_PLATE_DIR_RE)
        meta.append("plate")
    cyc_re = _CYCLE_DIR_RE if side == "sbs" else _ROUND_DIR_RE
    if re.search(cyc_re, rel):
        pieces.append(cyc_re)
        meta.append("cycle" if side == "sbs" else "round")
    pattern = r".*".join(pieces + [r"Index\.xml"]) if pieces else r"Index\.xml"
    return {
        "metadata_images_dir": str(images_dir),
        "metadata_path_pattern": pattern,
        "metadata_path_metadata": meta,
        "metadata_path_order_type": {c: int.__name__ for c in meta},
    }


def _read_nd2_meta(nd2_path: str | Path) -> dict:
    """Thin, mockable seam over nd2.ND2File. Returns a plain dict so tests can
    monkeypatch it without a binary fixture:

        {"channels": [name, ...], "n_z": int|None, "n_points": int|None,
         "voxel_um": (z, y, x)|None}

    Returns {} on any read failure (missing nd2, unreadable file) — the caller
    treats an empty meta as "could not inspect" and routes to the operator.
    """
    try:
        import nd2  # lazy — keeps brieflow_probe importable without nd2
    except Exception:
        return {}
    try:
        with nd2.ND2File(str(nd2_path)) as f:
            sizes = dict(getattr(f, "sizes", {}) or {})
            channels = [c.channel.name for c in f.metadata.channels] if f.metadata else []
            vox = getattr(f, "voxel_size", lambda: None)()
            voxel_um = (vox.z, vox.y, vox.x) if vox is not None else None
        return {
            "channels": list(channels),
            "n_z": sizes.get("Z"),
            "n_points": sizes.get("P"),
            "voxel_um": voxel_um,
        }
    except Exception:
        return {}


def _screen_channel_names(screen: dict, side: str) -> list[str]:
    """Declared channel names from screen.yaml for validation: iss.channels[].name
    (sbs) or phenotype.channels[].biological_name (phenotype)."""
    screen = screen or {}
    if side == "sbs":
        chans = ((screen.get("iss") or {}).get("channels")) or []
        return [c.get("name") for c in chans if isinstance(c, dict) and c.get("name")]
    chans = ((screen.get("phenotype") or {}).get("channels")) or []
    return [
        c.get("biological_name")
        for c in chans
        if isinstance(c, dict) and c.get("biological_name")
    ]


def _detect_channel_flip(
    embedded: list[str], declared: list[str]
) -> tuple[bool | None, str]:
    """Decide whether the ND2 container's channel order is reversed relative to the
    canonical declared CHANNEL_NAMES, anchored on DAPI.

    The pipeline assigns biological names to channels BY POSITION (after an optional
    `np.flip` of the channel axis), so if the container stores channels in the reverse
    of the declared order, `channel_order_flip` must be True or every channel is
    silently mislabeled (e.g. a far-red base channel labeled DAPI).

    Returns (flip, reason). `flip` is None when undecidable — caller routes the
    orientation to the operator rather than guessing.

    Multi-round phenotype: the ND2 reports ONE round's channels (e.g. 4) while
    screen.yaml declares the full multi-round stack (e.g. 8, with the reference
    channel listed per round: DAPI, DAPI_2). When the declared count is an exact
    multiple of the embedded count, orient against the per-round slice — anchored
    on DAPI, which every round carries, so the slice choice is orientation-safe.
    Without the per-round duplicate (declared 7 vs embedded 4 — doesn't divide),
    it stays undecidable and routes to the operator.
    """
    if not embedded or not declared:
        return None, "channel count mismatch / missing — cannot orient"

    per_round_declared = declared
    note_suffix = ""
    if len(embedded) != len(declared):
        if len(declared) > len(embedded) and len(declared) % len(embedded) == 0:
            per_round_declared = declared[: len(embedded)]
            note_suffix = (
                f" (per-round slice; {len(declared)} declared across "
                f"{len(declared) // len(embedded)} rounds)"
            )
        else:
            return None, "channel count mismatch / missing — cannot orient"

    def _dapi_idx(names: list[str]) -> int | None:
        hits = [i for i, n in enumerate(names) if n and "DAPI" in str(n).upper()]
        return hits[0] if len(hits) == 1 else None

    e, d = _dapi_idx(embedded), _dapi_idx(per_round_declared)
    if e is None or d is None:
        return None, "no unique DAPI channel to anchor orientation"
    if e == d:
        return False, f"DAPI aligned at index {d} — no flip{note_suffix}"
    if e == (len(embedded) - 1) - d:
        return True, f"DAPI at container index {e} vs declared {d} — reversed ⇒ flip{note_suffix}"
    return None, f"DAPI at container {e} vs declared {d} — not a clean reversal{note_suffix}"


def _probe_side(images_dir: str | Path, screen: dict, side: str) -> dict:
    """Probe one acquisition side (sbs | phenotype). Returns a detection dict:

        {"prefilled": {field: value}, "operator_needed": [field], "notes": [str]}

    Fields are bare (no sbs_/phenotype_ prefix); probe_preprocess prefixes them.
    Honest limits: nd2/zarr embed channels + z (read from metadata); tiff is
    per-tile with detectable z-planes but channels can't be inferred from the
    files → routed to the operator. Nothing is fabricated.
    """
    prefilled: dict = {}
    operator: list[str] = []
    notes: list[str] = []

    p = Path(images_dir)
    if not p.exists():
        return {
            "prefilled": {},
            "operator_needed": ["data_format", "channel_order"],
            "notes": [f"{side}: images dir {images_dir} not found — operator must stage data"],
        }

    fmt = detect_data_format(images_dir)
    prefilled["data_format"] = fmt

    # Split-tiff (Opera Phenix and similar) filenames carry no named
    # Well/Channel/Points tokens for infer_path_pattern to find below. Try the
    # generic prefix-hint inference first, over the FULL tiff listing (not
    # just a handful of samples) so channel_order/n_z_planes reflect what's
    # actually present rather than an arbitrary narrow slice. `layout` stays
    # None for every non-tiff format and for tiff conventions the classifier
    # can't resolve — the named-token block below is untouched either way.
    layout = None
    if fmt == "tiff":
        import glob

        all_rel = []
        for f in glob.glob(str(p / "**" / "*.tif*"), recursive=True):
            rel_path = Path(f).relative_to(p)
            if not _is_junk_path(rel_path.parts[:-1]):
                all_rel.append(str(rel_path))
        screen_channel_count = len(_screen_channel_names(screen, side) or []) or None
        layout = infer_split_tiff_layout(all_rel, side, screen_channel_count)

        # Opera Phenix keeps stage positions in an Index.xml in the image tree;
        # point the external-metadata vars at it so extract_metadata_tiff reads
        # real x_pos/y_pos instead of nulls (else merge has no coordinate seed).
        md = _index_xml_metadata_layout(p, side)
        if md:
            prefilled.update(md)
            notes.append(f"{side}: Opera Phenix Index.xml detected → external metadata via create_samples_df")

    if side == "sbs" and not has_cycle_dirs(p, ((screen or {}).get("iss") or {}).get("cycles")):
        return {
            "prefilled": {"data_format": fmt},
            "operator_needed": ["path_pattern"],
            "notes": [
                f"{side}: images under {images_dir} are not separated into one c<M>/ dir per "
                "sequencing cycle; stage them as plate_<N>/c<M>/ (setup's "
                "brieflow_validate_tree.py reports this as cycle-unseparated) — the cycle "
                "of a file is not read from its name"
            ],
        }

    samples = _sample_files(images_dir)
    pattern = None
    chan_tokens = None
    captures = None
    if samples:
        # Infer against the RELATIVE PATH, not just the basename. plate/cycle are
        # encoded in the directory structure (plate_<N>/c<M>/...), and the pipeline's
        # create_samples_df `.search()`es the FULL path — so the pattern must capture
        # those dirs too. Basename-only inference silently drops plate+cycle, which is
        # fatal for SBS (no cycle ⇒ all cycles of a well collapse together).
        rel = [str(s.relative_to(p)) for s in samples]
        cands = infer_path_pattern(rel)
        if cands:
            # channel tokens (tiff sides have no channel metadata) come from the top
            # candidate that actually captures a channel — kept separate so it doesn't
            # force a channel group into the structural path pattern below.
            chan_cand = next(
                (c for c in cands if "channel" in (c.get("captures") or [])), None
            )
            chan_tokens = (
                (chan_cand.get("distinct_values") or {}).get("channel") if chan_cand else None
            ) or None
            # Path pattern + metadata: prefer the best candidate whose captures are ALL
            # structural (plate/cycle/well/tile/round). create_samples_df zips the regex
            # groups positionally onto path_metadata, so an extra channel/seq group would
            # shift the columns and mislabel everything. Falls back to the top candidate.
            _STRUCT = {"plate", "well", "tile", "cycle", "round"}
            best = next(
                (c for c in cands if set(c.get("captures") or []) <= _STRUCT), cands[0]
            )
            pattern = best["regex"]
            prefilled["path_pattern"] = pattern
            captures = best.get("captures") or []
    if pattern is None and layout is None:
        operator.append("path_pattern")
        notes.append(f"{side}: could not infer a filename pattern — operator confirm")
    if samples and "well" not in (captures or []) and layout is None:
        wells = numeric_well_tokens(str(x.relative_to(p)) for x in samples)
        if wells:
            if "path_pattern" not in operator:
                operator.append("path_pattern")
            notes.append(
                f"{side}: filenames carry numeric wells (Well{wells[0]}..Well{wells[-1]}); "
                "brieflow needs row+column wells (A1) — operator maps them (e.g. a renamed "
                "symlink tree) and records the workaround with record_finding()"
            )

    # path_metadata + metadata_order_type are the structural capture fields from
    # the inferred pattern (plate/well/tile/cycle/round, excluding channel/seq);
    # well is a string token, the rest are ints.
    if captures:
        meta_fields = [c for c in captures if c in ("plate", "well", "tile", "cycle", "round")]
        if meta_fields:
            prefilled["path_metadata"] = meta_fields
            prefilled["metadata_order_type"] = {
                c: (str.__name__ if c == "well" else int.__name__) for c in meta_fields
            }
    # Baseline orientation: ascending/raw. For nd2 this is overridden below by the
    # DAPI-anchored auto-detect; tiff/zarr keep the ascending default.
    prefilled["channel_order_flip"] = False

    # round_order is a phenotype concept (sbs uses cycles); scan round_N dirs.
    if side == "phenotype":
        rounds = detect_round_order(images_dir, pattern)
        # The notebook takes None for a single round; [1] would filter on a round column a
        # single-round layout may not have.
        prefilled["round_order"] = rounds if len(rounds) > 1 else None
        notes.append(f"{side}: round_order={prefilled['round_order']} (round_N dirs; None = one round)")

    if fmt == "tiff":
        # Tiffs are one-FOV-per-file by construction (no embedded P axis).
        prefilled["data_organization"] = "tile"

        if layout is not None:
            prefilled["path_pattern"] = layout["path_pattern"]
            prefilled["path_metadata"] = layout["path_metadata"]
            prefilled["metadata_order_type"] = layout["metadata_order_type"]
            n_z = layout["n_z_planes"]
            if "z" in layout["path_metadata"]:
                # Authoritative full-tree count over the layout's own estimate,
                # which only reflects whatever files happened to be listed.
                full_z = detect_z_planes(
                    images_dir,
                    layout["path_pattern"],
                    fmt,
                    z_group_index=layout["path_metadata"].index("z"),
                )
                if full_z is not None:
                    n_z = full_z
            if n_z is not None:
                prefilled["n_z_planes"] = n_z
            if layout["channel_order"]:
                prefilled["channel_order"] = layout["channel_order"]
                notes.append(
                    f"{side}: tiff — channel_order={layout['channel_order']} from "
                    "split-tiff filename inference"
                )
            else:
                operator.append("channel_order")
                notes.append(
                    f"{side}: tiff — split-tiff layout resolved well/tile but no "
                    "unambiguous channel token → operator"
                )
            return {"prefilled": prefilled, "operator_needed": operator, "notes": notes}

        if pattern:
            z = detect_z_planes(images_dir, pattern, fmt)
            if z is not None:
                prefilled["n_z_planes"] = z
        # Tiff metadata carries no channels, but the filename Channel token does.
        if chan_tokens:
            prefilled["channel_order"] = chan_tokens
            notes.append(f"{side}: tiff — channel_order={chan_tokens} from filename Channel tokens")
        else:
            operator.append("channel_order")
            notes.append(f"{side}: tiff — no Channel token in filenames → operator")
        return {"prefilled": prefilled, "operator_needed": operator, "notes": notes}

    # nd2 / zarr: channels + z + organization live in the container metadata.
    prefilled["n_z_planes"] = None  # internal to the container
    org = detect_organization(images_dir, pattern, data_format=fmt)
    if org in ("well", "tile"):
        prefilled["data_organization"] = org
    else:
        operator.append("data_organization")

    if fmt == "nd2" and samples:
        metas = [_read_nd2_meta(s) for s in samples]
        metas = [m for m in metas if m.get("channels")]
        if metas:
            chans = metas[0]["channels"]
            # Uniformity: every sampled file must report the same channel list.
            if all(m["channels"] == chans for m in metas):
                prefilled["channel_order"] = None  # multichannel ND2 → embedded
                declared = _screen_channel_names(screen, side)
                if declared and len(declared) != len(chans):
                    if len(declared) % len(chans) == 0 and len(declared) > len(chans):
                        notes.append(
                            f"{side}: ND2 reports {len(chans)} channels/round {chans}; "
                            f"screen.yaml declares {len(declared)} across "
                            f"{len(declared) // len(chans)} rounds — multi-round, OK"
                        )
                    else:
                        notes.append(
                            f"{side}: ND2 has {len(chans)} channels {chans} but screen.yaml "
                            f"declares {len(declared)} {declared} — reconcile"
                        )
                else:
                    notes.append(f"{side}: {len(chans)} channels detected {chans} (uniform)")
                # Orientation auto-detect: the pipeline maps names→channels by POSITION,
                # so a container stored in reverse of the declared order needs a flip.
                flip, why = _detect_channel_flip(chans, declared)
                if flip is None:
                    prefilled.pop("channel_order_flip", None)
                    operator.append("channel_order_flip")
                    notes.append(
                        f"{side}: channel orientation undecided — {why} → confirm channel_order_flip"
                    )
                else:
                    prefilled["channel_order_flip"] = flip
                    notes.append(
                        f"{side}: channel_order_flip={flip} (container={chans} vs "
                        f"declared={declared}; {why})"
                    )
            else:
                operator.append("channel_order")
                notes.append(
                    f"{side}: channel info NOT uniform across files {[m['channels'] for m in metas]}"
                    " — operator must reconcile"
                )
        else:
            operator.append("channel_order")
            notes.append(f"{side}: could not read ND2 channel metadata → operator")
    elif fmt == "zarr":
        notes.append(f"{side}: zarr — channels/z embedded; not probed here → operator if needed")
        operator.append("channel_order")

    return {"prefilled": prefilled, "operator_needed": operator, "notes": notes}


def probe_preprocess(analysis_dir: str | Path, screen: dict) -> dict:
    """Comprehensive preprocess data probe — inspect the staged raw data for both
    acquisition sides and prefill the data-derivable preprocess interview rows.

    Composes the format / pattern / organization / z-plane probes and an
    ND2-metadata channel read (uniformity + screen.yaml reconciliation). It does
    NOT rebuild channel *names* — those come from screen.yaml derivations
    (channel_names / CHANNEL_CMAPS); this only fills what the *files* tell us.

    Returns::

        {"prefilled": {<interview_param>: value, ...},   # value_source=auto-probe
         "operator_needed": [<interview_param>, ...],     # couldn't infer → ask
         "notes": [str, ...]}                             # detections + warnings

    Param names are the real interview rows: sbs_data_format, SBS_PATH_PATTERN,
    sbs_data_organization, sbs_n_z_planes, sbs_channel_order (and phenotype_*).
    Empty/non-uniform/unreadable data → routed to operator; nothing fabricated.
    """
    data = (screen or {}).get("data") or {}
    # fieldmap values are the EXACT (uppercase) interview/notebook var names so
    # apply_preprocess_probe's set_value/find_param (case-sensitive) hit the rows.
    sides = {
        "sbs": (data.get("input_sbs_dir"), {
            "data_format": "SBS_DATA_FORMAT",
            "path_pattern": "SBS_PATH_PATTERN",
            "path_metadata": "SBS_PATH_METADATA",
            "metadata_order_type": "SBS_METADATA_ORDER_TYPE",
            "data_organization": "SBS_DATA_ORGANIZATION",
            "n_z_planes": "SBS_N_Z_PLANES",
            "channel_order": "SBS_CHANNEL_ORDER",
            "channel_order_flip": "SBS_CHANNEL_ORDER_FLIP",
            "metadata_images_dir": "SBS_METADATA_IMAGES_DIR_FP",
            "metadata_path_pattern": "SBS_METADATA_PATH_PATTERN",
            "metadata_path_metadata": "SBS_METADATA_PATH_METADATA",
            "metadata_path_order_type": "SBS_METADATA_PATH_ORDER_TYPE",
        }),
        "phenotype": (data.get("input_phenotype_dir"), {
            "data_format": "PHENOTYPE_DATA_FORMAT",
            "path_pattern": "PHENOTYPE_PATH_PATTERN",
            "path_metadata": "PHENOTYPE_PATH_METADATA",
            "metadata_order_type": "PHENOTYPE_METADATA_ORDER_TYPE",
            "data_organization": "PHENOTYPE_DATA_ORGANIZATION",
            "n_z_planes": "PHENOTYPE_N_Z_PLANES",
            "channel_order": "PHENOTYPE_CHANNEL_ORDER",
            "channel_order_flip": "PHENOTYPE_CHANNEL_ORDER_FLIP",
            "round_order": "PHENOTYPE_ROUND_ORDER",
            "metadata_images_dir": "PHENOTYPE_METADATA_IMAGES_DIR_FP",
            "metadata_path_pattern": "PHENOTYPE_METADATA_PATH_PATTERN",
            "metadata_path_metadata": "PHENOTYPE_METADATA_PATH_METADATA",
            "metadata_path_order_type": "PHENOTYPE_METADATA_PATH_ORDER_TYPE",
        }),
    }

    prefilled: dict = {}
    operator_needed: list[str] = []
    notes: list[str] = []
    base = Path(analysis_dir)

    # Fallback: if screen.yaml omits data.input_<side>_dir, use the operator-set
    # interview *_IMAGES_DIR_FP. The two sources diverge when the operator fills the
    # interview but not screen.yaml — probe_preprocess then produced None patterns and
    # the preprocess notebook crashed (re.compile(None)). OUTSTANDING #2 (kazbek hit it).
    _iv_dir = {"sbs": None, "phenotype": None}
    try:
        import json as _json
        _ivp = base / ".brieflow" / "interview.json"
        if _ivp.exists():
            _iv = _json.loads(_ivp.read_text())
            for _r in _iv.get("params", []):
                if _r.get("param_name") == "SBS_IMAGES_DIR_FP" and _r.get("value"):
                    _iv_dir["sbs"] = _r["value"]
                elif _r.get("param_name") == "PHENOTYPE_IMAGES_DIR_FP" and _r.get("value"):
                    _iv_dir["phenotype"] = _r["value"]
    except Exception:
        pass

    for side, (images_dir, fieldmap) in sides.items():
        if not images_dir and _iv_dir.get(side):
            images_dir = _iv_dir[side]
            notes.append(f"{side}: input dir from interview *_IMAGES_DIR_FP fallback ({images_dir})")
        if not images_dir:
            notes.append(f"{side}: no input dir in screen.yaml (data.input_{side}_dir) or interview — operator")
            continue
        # Resolve relative screen.yaml paths against the analysis dir.
        d = Path(images_dir)
        d = d if d.is_absolute() else base / d
        res = _probe_side(d, screen, side)
        for field, val in res["prefilled"].items():
            prefilled[fieldmap[field]] = val
        for field in res["operator_needed"]:
            if field in fieldmap:
                operator_needed.append(fieldmap[field])
        notes.extend(res["notes"])

    return {"prefilled": prefilled, "operator_needed": operator_needed, "notes": notes}


def _index_xml_pixel_size_um(images_dir: str | Path) -> float | None:
    """y-pixel size (µm) from an Opera Phenix `Index.xml` under `images_dir`.

    Harmony records `<ImageResolutionY Unit="m">` per image, already accounting for
    objective and binning, so the first entry describes the whole acquisition. Read
    line-wise: a 60-well Index.xml runs to tens of MB and only the first hit matters.
    Returns None when the tree holds no `Index.xml` or no resolution tag.
    """
    import glob

    hits = glob.glob(str(Path(images_dir) / "**" / "Index.xml"), recursive=True)
    if not hits:
        return None
    try:
        with open(hits[0], errors="replace") as fh:
            for line in fh:
                found = re.search(r"<ImageResolutionY[^>]*>([^<]+)<", line)
                if found:
                    return float(found.group(1)) * 1e6
    except (OSError, ValueError):
        return None
    return None


def _side_pixel_size_um(analysis_dir: str | Path, images_dir, screen: dict) -> float | None:
    """Median y-pixel size (µm) of sample ND2s under `images_dir`, falling back to an
    Opera Phenix `Index.xml` for split-tiff screens, or None."""
    if not images_dir:
        return None
    d = Path(images_dir)
    d = d if d.is_absolute() else Path(analysis_dir) / d
    # Median over several files, so one tile with wrong metadata (a blank field) can't set it.
    files = _sample_files(d, n=100_000)
    sizes = []
    for f in files[:: max(1, len(files) // 7)][:7]:
        if f.suffix.lower() == ".nd2":
            vox = _read_nd2_meta(f).get("voxel_um")
            if vox and vox[1]:
                sizes.append(float(vox[1]))
    if sizes:
        return float(sorted(sizes)[len(sizes) // 2])
    return _index_xml_pixel_size_um(d)


# Calibrated microscope pixel sizes outside this band (µm/px) are metadata errors, not optics.
PIXEL_SIZE_UM_RANGE = (0.05, 5.0)


def probe_det_range(analysis_dir: str | Path, screen: dict) -> dict:
    """Merge DET_RANGE from the ph/sbs pixel-size ratio (ND2 voxel sizes, else a
    Phenix Index.xml). DET_RANGE gates the DETERMINANT of the
    2D phenotype↔SBS affine transform (`np.linalg.det` of the 2x2 = the AREA scale =
    linear_scale**2). The linear scale is the pixel-size ratio M*B = ph_px/sbs_px,
    so the determinant is (M*B)**2 and DET_RANGE = [0.9*(M*B)**2, 1.1*(M*B)**2].
    (The earlier formula used 1/(M*B) — inverted AND un-squared — which put the band
    ~2 orders of magnitude off, e.g. [3.6,4.4] vs [0.056,0.069], so 0 alignment sites
    passed on merge.) Returns {"DET_RANGE": [lo, hi], "notes": [...]} or
    {"DET_RANGE": None, "notes": [...]} when a pixel size can't be read."""
    data = (screen or {}).get("data") or {}
    sbs_px = _side_pixel_size_um(analysis_dir, data.get("input_sbs_dir"), screen)
    ph_px = _side_pixel_size_um(analysis_dir, data.get("input_phenotype_dir"), screen)
    if not sbs_px or not ph_px:
        return {"DET_RANGE": None,
                "notes": [f"DET_RANGE: need ND2 voxel sizes or a Phenix Index.xml "
                          f"both sides (sbs={sbs_px}, ph={ph_px}) — left for operator"]}
    lo, hi = PIXEL_SIZE_UM_RANGE
    bad = {k: v for k, v in (("sbs", sbs_px), ("ph", ph_px)) if not lo <= v <= hi}
    if bad:
        return {"DET_RANGE": None,
                "notes": [f"DET_RANGE: implausible pixel size {bad} µm (expected {lo}-{hi}) "
                          "— check the image metadata; left for operator"]}
    mb = ph_px / sbs_px
    det = [round(0.9 * mb**2, 4), round(1.1 * mb**2, 4)]
    return {"DET_RANGE": det,
            "notes": [f"DET_RANGE={det} from determinant=(M*B)**2={mb**2:.4f} "
                      f"(M*B={mb:.4f}; ph {ph_px}µm / sbs {sbs_px}µm)"]}


def probe_phenotype_cells_per_tile(analysis_dir: str | Path, max_wells: int = 8) -> dict:
    """Median phenotype cells per tile from the per-well `phenotype_info` parquets.

    Reads the `tile` column of up to `max_wells` wells (tiff and zarr layouts). Tiles with
    no cell are absent from the tables, so the median is over tiles with at least one.
    Returns {"median_cells_per_tile": float | None, "n_tiles": int, "n_wells": int,
    "notes": [...]}; None until phenotype has produced its info tables.
    """
    import statistics

    root = Path(analysis_dir) / "brieflow_output" / "phenotype" / "parquets"
    files = sorted(root.rglob("*phenotype_info.parquet")) if root.is_dir() else []
    if not files:
        return {"median_cells_per_tile": None, "n_tiles": 0, "n_wells": 0,
                "notes": ["no phenotype_info parquets yet"]}
    import pandas as pd

    counts: list[int] = []
    read = 0
    for f in files[:max_wells]:
        try:
            counts += pd.read_parquet(f, columns=["tile"]).groupby("tile").size().tolist()
            read += 1
        except Exception:
            continue
    if not counts:
        return {"median_cells_per_tile": None, "n_tiles": 0, "n_wells": 0,
                "notes": ["phenotype_info parquets unreadable"]}
    median = float(statistics.median(counts))
    return {"median_cells_per_tile": median, "n_tiles": len(counts), "n_wells": read,
            "notes": [f"median {median:g} cells over {len(counts)} tiles in {read} wells"]}
