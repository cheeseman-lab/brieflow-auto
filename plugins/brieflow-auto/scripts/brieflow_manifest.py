#!/usr/bin/env python3
"""brieflow_manifest.py — run_manifest.json: which plugin, brieflow, screen repo and
interpreter produced this run. A version stamp for a project in evolution, not a
reproducibility guarantee. Every field is best-effort; nothing here raises."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MANIFEST_SCHEMA = "brieflow-run-manifest/v1"
MANIFEST_NAME = "run_manifest.json"
_ENV_KEYS = ("BRIEFLOW_STOP_AFTER", "BRIEFLOW_QC_ADVISORY", "BRIEFLOW_SKIP_DISK_CHECK")


def write_run_manifest(analysis_dir: Path | str, args, tag: str) -> Path:
    """Write .brieflow/run_manifest.json for this launch and return its path."""
    analysis_dir = Path(analysis_dir).resolve()
    out = analysis_dir / ".brieflow" / MANIFEST_NAME
    screen_yaml = analysis_dir / "screen.yaml"
    repo = _git(analysis_dir, "rev-parse", "--show-toplevel")
    doc = {
        "schema": MANIFEST_SCHEMA,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "plugin": {
            "version": plugin_version(),
            "path": str(SCRIPT_DIR),
            "git_sha": _git(SCRIPT_DIR, "rev-parse", "HEAD"),
            "health": plugin_health(),
        },
        "screen": {
            "repo_sha": _git(analysis_dir, "rev-parse", "HEAD"),
            "brieflow_submodule_sha": _git(Path(repo) / "brieflow", "rev-parse", "HEAD")
            if repo
            else None,
            "screen_yaml_sha256": _sha256(screen_yaml),
            "analysis_dir": str(analysis_dir),
        },
        "env": {
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "snakemake": _dist_version("snakemake"),
            "marimo": _dist_version("marimo"),
            "brieflow": _dist_version("brieflow"),
            "hostname": socket.gethostname(),
        },
        "run": {
            "backend": getattr(args, "backend", None),
            "lifecycle_hooks": getattr(args, "lifecycle_hooks", "noop"),
            "screen_id": getattr(args, "screen_id", None) or analysis_dir.name,
            "from": getattr(args, "from_phase", None),
            "through": getattr(args, "through", None),
            "only": getattr(args, "only", None),
            "accept_qc": getattr(args, "accept_qc", None),
            "accept_layout": getattr(args, "accept_layout", False),
            "argv": list(sys.argv),
            "env_overrides": {k: os.environ.get(k) for k in _ENV_KEYS if os.environ.get(k)},
        },
    }
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=2))
    except OSError as e:
        print(f"brieflow_manifest: could not write {out}: {e}", file=sys.stderr)
    return out


def read_run_manifest(analysis_dir: Path | str) -> dict | None:
    p = Path(analysis_dir) / ".brieflow" / MANIFEST_NAME
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def sacct_start(analysis_dir: Path | str, *, fallback_hours: int = 48) -> str:
    """Local start time for sacct queries covering the current launch, across midnight.

    `--starttime=today` drops jobs that failed before midnight on an overnight run; this
    uses the launch's manifest timestamp, else the last fallback_hours."""
    written = (read_run_manifest(analysis_dir) or {}).get("written_at")
    try:
        start = datetime.fromisoformat(written).astimezone()
    except (TypeError, ValueError):
        start = datetime.now().astimezone() - timedelta(hours=fallback_hours)
    return start.strftime("%Y-%m-%dT%H:%M:%S")


def plugin_version() -> str | None:
    try:
        doc = json.loads((SCRIPT_DIR.parent / ".claude-plugin" / "plugin.json").read_text())
    except (OSError, ValueError):
        return None
    return doc.get("version") if isinstance(doc, dict) else None


def plugin_health(home: Path | None = None) -> list[str]:
    """Problems with this plugin's Claude Code install, as operator-readable lines.

    Checks the registry files parse (a NUL-filled known_marketplaces.json left the plugin
    unloadable) and that the running version is the one installed for the user."""
    registry = (home or Path.home()) / ".claude" / "plugins"
    issues: list[str] = []
    docs = {}
    for name in ("known_marketplaces.json", "installed_plugins.json"):
        path = registry / name
        if not path.exists():
            continue
        try:
            docs[name] = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            issues.append(f"{path} is unreadable ({type(e).__name__}); re-add the marketplace")
    running = plugin_version()
    installed = (docs.get("installed_plugins.json") or {}).get("plugins", {})
    entries = installed.get("brieflow-auto@brieflow-auto") if isinstance(installed, dict) else None
    versions = {e.get("version") for e in entries or [] if isinstance(e, dict)}
    if running and versions and running not in versions:
        issues.append(
            f"running brieflow-auto {running} but installed {sorted(versions)}; "
            "/reload-plugins or reinstall"
        )
    return issues


def _git(cwd: Path, *argv: str) -> str | None:
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), *argv], capture_output=True, text=True, timeout=10
        )
        return (r.stdout.strip() or None) if r.returncode == 0 else None
    except Exception:
        return None


def _dist_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except Exception:
        return None


def _sha256(p: Path) -> str | None:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Print values derived from run_manifest.json")
    parser.add_argument("--analysis-dir", required=True)
    parser.add_argument("--sacct-start", action="store_true", required=True)
    args = parser.parse_args()
    print(sacct_start(args.analysis_dir))


if __name__ == "__main__":
    main()
