#!/usr/bin/env python3
"""brieflow_reorder_channels.py — permute the channel axis of an already-converted
brieflow preprocess store IN PLACE, so a screen whose channels were converted in
the wrong order (e.g. DAPI last, when the pipeline convention is DAPI first) can be
fixed without re-running preprocess.

Operates on every OME-Zarr array under a store root whose channel axis (axis 1 of
the t,c,z,y,x layout) has the expected length: the image store
(``preprocess/<phase>/image_<plate>.zarr``) and, run separately, the matching IC
fields (``preprocess/ic_fields/<phase>``), which are per-channel and must be
permuted identically.

Safety model:
  * dry-run by default — ``--apply`` is required to write anything;
  * idempotent / resumable — each array's parent group gets a
    ``brieflow_channel_order`` attribute recording the order it now holds; arrays
    already in the target order are skipped, so a re-run after an interruption
    only touches what is left;
  * self-verifying — per-channel means are computed before and after each write
    and must match under the permutation, else the array is reported.

The store must NOT be in use by a running pipeline or notebook kernel.

After applying, update the screen's provenance to match (printed at the end):
``config.yml`` ``<phase>_channel_order`` / ``channel_names``, ``screen.yaml``'s
channel list, and re-resolve the interview.

CLI:
  --store PATH        store root to walk (image_*.zarr or ic_fields/<phase>)
  --from A,B,C        the TRUE names of the stored channels, in stored (physical)
                      order — not the possibly-mislabelled names in config.yml. A
                      name↔channel mislabel is fixed in config alone (no data
                      moves); only a real reordering (e.g. DAPI-first) moves data.
  --to   C,A,B        desired channel names (a permutation of --from)
  --apply             write (default: dry-run report only)
  --jobs N            parallel workers (I/O bound; default 8)
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ATTR = "brieflow_channel_order"


def find_arrays(store: Path, n_channels: int) -> list[Path]:
    """Return the group dirs (parent of the ``0`` array) of every zarr array under
    `store` whose channel axis has length `n_channels`."""
    found: list[Path] = []
    for meta in store.rglob("zarr.json"):
        if meta.parent.name != "0":
            continue
        try:
            j = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        shape = j.get("shape")
        if j.get("node_type") == "array" and shape and len(shape) == 5 and shape[1] == n_channels:
            found.append(meta.parent.parent)
    return sorted(found)


def current_order(group: Path, default: list[str]) -> list[str]:
    meta = group / "zarr.json"
    try:
        attrs = json.loads(meta.read_text()).get("attributes", {})
    except (OSError, ValueError):
        return default
    return list(attrs.get(ATTR) or default)


def record_order(group: Path, order: list[str]) -> None:
    meta = group / "zarr.json"
    j = json.loads(meta.read_text())
    j.setdefault("attributes", {})[ATTR] = list(order)
    meta.write_text(json.dumps(j, indent=2))


def permute_array(group: Path, perm: list[int]) -> tuple[bool, str]:
    """Permute axis 1 of ``group/0`` in place. Returns (ok, message)."""
    import numpy as np
    import zarr

    arr = zarr.open_array(str(group / "0"), mode="r+")
    data = arr[:]
    before = data.mean(axis=(0, 2, 3, 4))
    data = data[:, perm]
    arr[:] = data
    after = zarr.open_array(str(group / "0"), mode="r")[:].mean(axis=(0, 2, 3, 4))
    if not np.allclose(after, before[perm]):
        return False, f"{group}: per-channel means do not match after permutation"
    return True, ""


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--from", dest="src", required=True, help="current names, stored order")
    p.add_argument("--to", dest="dst", required=True, help="desired names")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--jobs", type=int, default=8)
    args = p.parse_args()

    src = [s.strip() for s in args.src.split(",")]
    dst = [s.strip() for s in args.dst.split(",")]
    if sorted(src) != sorted(dst) or len(set(src)) != len(src):
        print(f"ERROR: --to must be a permutation of --from ({src} vs {dst})", file=sys.stderr)
        return 2
    perm = [src.index(name) for name in dst]

    groups = find_arrays(args.store, len(src))
    todo = [g for g in groups if current_order(g, src) != dst]
    print(f"store: {args.store}")
    print(f"arrays with a {len(src)}-channel axis: {len(groups)}; already in target order: "
          f"{len(groups) - len(todo)}; to permute: {len(todo)}  (perm {src} -> {dst} = {perm})")
    if not args.apply:
        print("dry-run — pass --apply to write.")
        return 0

    failures: list[str] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(permute_array, g, perm): g for g in todo}
        for fut in as_completed(futs):
            g = futs[fut]
            ok, msg = fut.result()
            if ok:
                record_order(g, dst)
                done += 1
                if done % 500 == 0:
                    print(f"  {done}/{len(todo)} permuted", flush=True)
            else:
                failures.append(msg)
    print(f"permuted {done}/{len(todo)} arrays; failures: {len(failures)}")
    for m in failures[:20]:
        print("  FAIL", m)
    print("\nNow update provenance to match: config.yml <phase>_channel_order + channel_names,"
          " screen.yaml channel list, then re-resolve the interview.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
