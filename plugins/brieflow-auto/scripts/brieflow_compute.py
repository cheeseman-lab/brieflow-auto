#!/usr/bin/env python3
"""
brieflow_compute.py — establish the execution backend + slurm identity by probing
the environment, so brieflow-auto runs invariantly on GCS (local) and HPC (slurm)
without a hardcoded, WIBR-specific base slurm profile.

probe_compute() detects the backend (sbatch present → slurm, else local) and, on
slurm, the candidate partitions (sinfo) and accounts (sacctmgr). The operator picks
the partition (never guessed — the cluster default is often not the one you run on).
write_base_slurm_profile() then materializes analysis/slurm/config.yaml from the
established values. No set-resources block: per-rule mem is sized at run time by
brieflow_resolve_resources (mem_recommendations baseline + scaling_formulas).

CLI:
  --probe                          emit {backend, partitions, partition_default,
                                   accounts} as JSON
  --write --analysis-dir D --partition P [--account A] [--jobs N]
                                   write D/slurm/config.yaml (slurm only)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# General (not WIBR-specific) execution defaults — operator-editable in the file.
DEFAULT_JOBS = 100
DEFAULT_RUNTIME = "1d"
DEFAULT_MEM_MB = 3000


# ---------------------------------------------------------------------------
# Environment seams — thin, mockable wrappers over the slurm CLIs.
# ---------------------------------------------------------------------------


def _sbatch_available() -> bool:
    return shutil.which("sbatch") is not None


def _run(cmd: list[str], timeout: int = 10) -> str | None:
    """Run a command, return stdout or None on any failure. Never raises."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _probe_partitions() -> tuple[list[str], str | None]:
    """Return (partitions, default_partition). sinfo marks the default with `*`."""
    out = _run(["sinfo", "-h", "-o", "%P"])
    if out is None:
        return [], None
    partitions: list[str] = []
    default: str | None = None
    for tok in out.split():
        name = tok.rstrip("*")
        if not name:
            continue
        if tok.endswith("*"):
            default = name
        if name not in partitions:
            partitions.append(name)
    return partitions, default


def _probe_accounts() -> list[str]:
    """Return the invoking user's slurm accounts (deduped, order-preserved)."""
    import os

    user = os.environ.get("USER") or ""
    out = _run(
        ["sacctmgr", "-n", "-P", "show", "assoc", f"user={user}", "format=account"]
    )
    if out is None:
        return []
    accounts: list[str] = []
    for line in out.splitlines():
        acct = line.strip().rstrip("|").strip()
        if acct and acct not in accounts:
            accounts.append(acct)
    return accounts


# ---------------------------------------------------------------------------
# Probe + materialize
# ---------------------------------------------------------------------------


def probe_compute() -> dict:
    """Detect the execution backend and slurm identity candidates.

    Returns::

        {"backend": "local"}                       # no sbatch
        {"backend": "slurm",                        # sbatch present
         "partitions": [str, ...],                  # sinfo candidates
         "partition_default": str | None,           # cluster default (never auto-used)
         "accounts": [str, ...]}                    # this user's sacctmgr accounts

    The operator chooses the partition; account is auto only when a single candidate.
    Nothing is guessed — an empty candidate list routes to the operator.
    """
    if not _sbatch_available():
        return {"backend": "local"}
    partitions, default = _probe_partitions()
    return {
        "backend": "slurm",
        "partitions": partitions,
        "partition_default": default,
        "accounts": _probe_accounts(),
    }


def write_base_slurm_profile(
    analysis_dir: str | Path,
    *,
    backend: str,
    partition: str | None = None,
    account: str | None = None,
    jobs: int = DEFAULT_JOBS,
    runtime: str = DEFAULT_RUNTIME,
    mem_mb: int = DEFAULT_MEM_MB,
) -> Path | None:
    """Write analysis/slurm/config.yaml from established compute identity.

    Slurm: writes the base profile (default-resources + jobs) and returns its path.
    Local: writes nothing (no slurm profile is needed) and returns None.
    Raises ValueError if slurm is requested without a partition — it must be chosen,
    not defaulted.
    """
    if backend == "local":
        return None
    if backend != "slurm":
        raise ValueError(f"unknown backend: {backend!r} (choose: slurm, local)")
    if not partition:
        raise ValueError("slurm backend requires a partition (operator choice)")

    lines = [
        "# BrieFlow slurm profile — base execution identity (generated by",
        "# brieflow_compute from a probe; partition operator-chosen). Per-rule mem is",
        "# sized at run time by brieflow_resolve_resources, so there is no",
        "# set-resources block. Edit slurm_partition/slurm_account for another cluster.",
        "",
        "default-resources:",
        f'    slurm_partition: "{partition}"',
    ]
    if account:
        lines.append(f'    slurm_account: "{account}"')
    lines += [
        f"    mem_mb: {mem_mb}",
        "    tasks: 1",
        "    cpus_per_task: 1",
        f'    runtime: "{runtime}"',
        f"jobs: {jobs}",
        "",
    ]
    out = Path(analysis_dir) / "slurm" / "config.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--probe", action="store_true", help="emit backend/identity JSON")
    p.add_argument("--write", action="store_true", help="write slurm/config.yaml")
    p.add_argument("--analysis-dir", type=Path)
    p.add_argument("--backend", choices=["local", "slurm"])
    p.add_argument("--partition")
    p.add_argument("--account")
    p.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    args = p.parse_args()

    if args.probe:
        json.dump(probe_compute(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if args.write:
        if not args.analysis_dir or not args.backend:
            print("ERROR: --write needs --analysis-dir and --backend", file=sys.stderr)
            return 2
        out = write_base_slurm_profile(
            args.analysis_dir,
            backend=args.backend,
            partition=args.partition,
            account=args.account,
            jobs=args.jobs,
        )
        print("local backend — no slurm profile written" if out is None else f"wrote {out}")
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
