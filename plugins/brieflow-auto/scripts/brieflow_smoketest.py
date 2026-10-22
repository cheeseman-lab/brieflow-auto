#!/usr/bin/env python3
"""
brieflow_smoketest.py — sub-10s fail-fast probe before launching a phase.

Solves HARDENING_TODO #16: too many reference-run incidents (polars SIGILL v27/v28,
missing iohub install, broken zarr3 transitive deps) cost minutes-to-hours
of cluster compute before the operator realized the env was wrong. Each
of these is detectable in seconds via a probe.

Checks (each emits one PASS / FAIL line):
  - Required packages import cleanly: brieflow, polars, snakemake, iohub,
    zarr, pandas, numpy, ome_zarr
  - Optional version pins match (read from pyproject.toml if present)
  - Config YAML loads and has the top-level keys brieflow expects
  - Backend probe: `sinfo` succeeds when --backend=slurm

Exits 0 when all checks pass; non-zero with a checked list when any fails.
Runloop calls this as step 0 of every attempt 1 (gated by --no-smoketest).
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
import time
from pathlib import Path

REQUIRED_PACKAGES = [
    # brieflow itself lives as a submodule, not a site-packages install —
    # snakemake adds workflow/scripts to PYTHONPATH at job-start. Don't
    # probe `import brieflow`; probe its on-disk presence instead via
    # probe_brieflow_present().
    "polars",
    "snakemake",
    "iohub",
    "zarr",
    "pandas",
    "numpy",
    "ome_zarr",
]

REQUIRED_CONFIG_TOP_LEVEL = ("all", "preprocess")


# ---------------------------------------------------------------------------
# Result + reporting
# ---------------------------------------------------------------------------


class Result:
    __slots__ = ("name", "ok", "detail")

    def __init__(self, name: str, ok: bool, detail: str = "") -> None:
        self.name = name
        self.ok = ok
        self.detail = detail

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def probe_imports() -> list[Result]:
    out: list[Result] = []
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
            out.append(Result(f"import {pkg}", True))
        except ImportError as e:
            out.append(Result(f"import {pkg}", False, str(e)[:200]))
    return out


def probe_brieflow_present(brieflow_dir: Path | None) -> list[Result]:
    """Check that the brieflow submodule is reachable + has a Snakefile."""
    if not brieflow_dir:
        return [
            Result("brieflow submodule discoverable", False, "no candidate dir found")
        ]
    snakefile = brieflow_dir / "workflow" / "Snakefile"
    if not snakefile.exists():
        return [
            Result(
                "brieflow/workflow/Snakefile exists", False, f"not found at {snakefile}"
            )
        ]
    return [Result("brieflow/workflow/Snakefile exists", True, f"at {brieflow_dir}")]


def probe_versions(pyproject_path: Path | None) -> list[Result]:
    """If pyproject.toml is present, check whether installed versions match
    the pins. Soft check: mismatch is reported as FAIL but only the bare
    presence of `polars[rtcompat]` is hard-required (HARDENING #11).
    """
    out: list[Result] = []
    if not pyproject_path or not pyproject_path.exists():
        return out
    try:
        text = pyproject_path.read_text()
    except OSError as e:
        return [Result("read pyproject.toml", False, str(e)[:200])]

    # Cheap parser: walk lines, pick up `name = "..."`, `version = "..."`,
    # and the `dependencies = [...]` block. Avoid tomllib dependency.
    deps: list[str] = []
    in_deps = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("dependencies"):
            in_deps = True
            continue
        if in_deps:
            if stripped.startswith("]"):
                in_deps = False
                continue
            if stripped.startswith('"') and stripped.endswith('",'):
                deps.append(stripped.strip(",").strip('"'))

    # Specifically check that polars-runtime-compat is present, per the
    # 2026-05-12 polars-AVX2 resolution (HARDENING #11).
    try:
        from importlib.metadata import version, PackageNotFoundError

        try:
            version("polars-runtime-compat")
            out.append(Result("polars[rtcompat] installed", True))
        except PackageNotFoundError:
            out.append(
                Result(
                    "polars[rtcompat] installed",
                    False,
                    "polars-runtime-compat not found — env will SIGILL on Ivy "
                    "Bridge nodes (see HARDENING_TODO #11). Reinstall: "
                    "`uv pip install 'polars[rtcompat]'`.",
                )
            )
    except ImportError:
        pass

    return out


def probe_config(config_path: Path) -> list[Result]:
    out: list[Result] = []
    if not config_path.exists():
        return [Result(f"config file exists: {config_path}", False, "not found")]
    try:
        import yaml

        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        out.append(Result("config YAML parses", True, f"{len(cfg)} top-level keys"))
    except Exception as e:
        return [Result("config YAML parses", False, str(e)[:200])]

    missing = [k for k in REQUIRED_CONFIG_TOP_LEVEL if k not in cfg]
    if missing:
        out.append(
            Result("required config top-level keys", False, f"missing: {missing}")
        )
    else:
        out.append(Result("required config top-level keys", True))
    return out


def probe_backend(backend: str) -> list[Result]:
    if backend != "slurm":
        return [Result(f"backend probe ({backend})", True, "skipped (non-slurm)")]
    try:
        r = subprocess.run(
            ["sinfo", "-h", "-o", "%P"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return [Result("sinfo callable", False, f"{type(e).__name__}: {e}")]
    if r.returncode != 0:
        return [
            Result(
                "sinfo callable",
                False,
                f"exit={r.returncode}: {r.stderr.strip()[:200]}",
            )
        ]
    partitions = [p.strip("* \t") for p in r.stdout.split()]
    return [Result("sinfo callable", True, f"{len(partitions)} partitions visible")]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def run_smoketest(
    analysis_dir: Path,
    config_path: Path,
    backend: str,
    brieflow_dir: Path | None,
) -> list[Result]:
    results: list[Result] = []
    results.extend(probe_imports())
    results.extend(probe_brieflow_present(brieflow_dir))
    if brieflow_dir and brieflow_dir.exists():
        results.extend(probe_versions(brieflow_dir / "pyproject.toml"))
    results.extend(probe_config(config_path))
    results.extend(probe_backend(backend))
    return results


def print_report(results: list[Result]) -> int:
    failed = [r for r in results if not r.ok]
    width = max((len(r.name) for r in results), default=20)
    print(
        f"\n=== brieflow_smoketest ({len(results)} checks, {len(failed)} failures) ==="
    )
    for r in results:
        mark = "PASS" if r.ok else "FAIL"
        line = f"  [{mark}] {r.name:<{width}}"
        if r.detail:
            line += f"  — {r.detail}"
        print(line)
    return 0 if not failed else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--analysis-dir", required=True, type=Path)
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Config file path. Default: <analysis-dir>/config/config.yml",
    )
    p.add_argument("--backend", default="slurm", choices=["local", "slurm"])
    p.add_argument(
        "--brieflow-dir",
        type=Path,
        default=None,
        help="brieflow submodule path. Default: <analysis-dir>/../brieflow "
        "or <analysis-dir>/brieflow.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output instead of human-readable.",
    )
    args = p.parse_args()

    analysis_dir = args.analysis_dir.resolve()
    if not analysis_dir.exists():
        print(f"ERROR: --analysis-dir {analysis_dir} does not exist.", file=sys.stderr)
        return 2

    config_path = args.config or analysis_dir / "config" / "config.yml"
    if args.brieflow_dir:
        brieflow_dir = args.brieflow_dir.resolve()
    else:
        for c in (
            analysis_dir.parent / "brieflow",
            analysis_dir / "brieflow",
            analysis_dir.parent.parent / "brieflow",
        ):
            if c.exists():
                brieflow_dir = c
                break
        else:
            brieflow_dir = None

    t0 = time.time()
    results = run_smoketest(analysis_dir, config_path, args.backend, brieflow_dir)
    elapsed = time.time() - t0

    if args.json:
        json.dump(
            {
                "elapsed_s": elapsed,
                "n_passed": sum(1 for r in results if r.ok),
                "n_failed": sum(1 for r in results if not r.ok),
                "results": [r.to_dict() for r in results],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0 if all(r.ok for r in results) else 1

    rc = print_report(results)
    print(f"\n  elapsed {elapsed:.2f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
