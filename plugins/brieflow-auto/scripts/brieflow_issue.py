#!/usr/bin/env python3
"""Prepare and open Brieflow bug reports, retaining a local submission receipt.

The agent supplies reviewed, shareable evidence; raw logs and manifests are never
uploaded. The legacy issue_draft files remain local inputs, not the workflow's endpoint.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from brieflow_decisions import record_decision
from brieflow_issue_draft import FILL, UPSTREAM_REPO
from brieflow_manifest import read_run_manifest

SCHEMA = "brieflow-issue-report/v1"
TEMPLATE_URL = (
    "https://github.com/cheeseman-lab/brieflow/blob/main/.github/ISSUE_TEMPLATE/bug_report.md"
)


def _save(path: Path, doc: dict) -> None:
    """Replace atomically, with temporary state next to the report on the same filesystem."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(doc, indent=2) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _text(value: str, name: str) -> str:
    value = value.strip()
    if not value or FILL in value or re.search(r"\b(TODO|PENDING)\b", value):
        raise ValueError(f"Complete {name} before preparing an issue")
    return value


def prepare_report(
    analysis_dir: Path,
    *,
    title: str,
    description: str,
    reproduction: str,
    expected_behavior: str,
    environment: str,
    context: str,
    proposed_fix: str,
    compatibility: str,
    verification: str,
    screenshots: str = "Not applicable (command-line pipeline failure).",
) -> Path:
    """Render upstream's bug template using explicitly selected public content.

    Keep failure-time SHAs from the manifest. A current checkout can differ from the
    failing run and must not silently replace its provenance.
    """
    analysis_dir = Path(analysis_dir).resolve()
    title = _text(title, "title")
    if not title.startswith("[BUG] "):
        title = "[BUG] " + title
    if "\n" in title or len(title) > 256:
        raise ValueError("Issue title must be one line and at most 256 characters")
    manifest = read_run_manifest(analysis_dir) or {}
    screen = manifest.get("screen") or {}
    sha = screen.get("brieflow_submodule_sha")
    repo_sha = screen.get("repo_sha")
    if not all(re.fullmatch(r"[0-9a-f]{40}", str(v or "")) for v in (sha, repo_sha)):
        raise ValueError("A run manifest with full Brieflow and screen commit SHAs is required")
    sections = [
        ("Describe the bug", description),
        ("To Reproduce", reproduction),
        ("Expected behavior", expected_behavior),
        ("Screenshots", screenshots),
        ("Desktop (please complete the following information):", environment),
        ("Additional context", context),
    ]
    body = "\n\n".join(f"**{name}**\n{_text(value, name)}" for name, value in sections)
    body += "\n\n" + "\n\n".join(
        f"### {name}\n{_text(value, name)}"
        for name, value in (
            ("Proposed fix (not applied)", proposed_fix),
            ("Backward compatibility", compatibility),
            ("Regression tests and verification", verification),
        )
    )
    body += f"\n\n- brieflow commit: {sha}\n- Screen repo commit: {repo_sha}\n"
    digest = hashlib.sha256(f"{UPSTREAM_REPO}\n{title}\n{body}".encode()).hexdigest()
    marker = f"<!-- brieflow-auto-report:{digest} -->"
    doc = {
        "schema": SCHEMA,
        "repo": UPSTREAM_REPO,
        "template_url": TEMPLATE_URL,
        "title": title,
        "body": body + "\n" + marker + "\n",
        "fingerprint": digest,
        "analysis_dir": str(analysis_dir),
        "status": "prepared",
        "filed_url": None,
    }
    folder = analysis_dir / ".brieflow" / "issues"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest}.json"
    # Concurrent preparation must not overwrite a submission receipt.
    with (folder / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists():
            _save(path, doc)
    return path


def _gh(*args: str, body: str | None = None) -> str:
    result = subprocess.run(
        ["gh", *args],
        input=body,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "GH_PROMPT_DISABLED": "1", "GH_HOST": "github.com"},
    )
    if result.returncode:
        # Do not echo CLI diagnostics: they can include private context or credentials.
        raise RuntimeError(
            f"GitHub command failed (exit {result.returncode}); check gh authentication/access"
        )
    return result.stdout.strip()


def _url(value: str) -> str:
    if not re.fullmatch(r"https://github\.com/cheeseman-lab/brieflow/issues/[1-9][0-9]*", value):
        raise ValueError("GitHub did not return a Brieflow issue URL")
    return value


def open_report(path: Path) -> str:
    """Open the agent's completed report once, or recover the existing issue's URL.

    The issue skill checks defect scope, public content, and compatibility before
    invoking this function. No per-issue operator confirmation is required. An uncertain
    submission is never blindly retried: a timeout can follow successful creation.
    """
    path = Path(path).resolve()
    with (path.parent / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        doc = json.loads(path.read_text())
        if doc.get("schema") != SCHEMA or doc.get("repo") != UPSTREAM_REPO:
            raise ValueError("Not a Brieflow issue report")
        digest = doc["fingerprint"]
        marker = f"<!-- brieflow-auto-report:{digest} -->"
        body, separator, tail = doc["body"].rpartition("\n" + marker + "\n")
        expected = hashlib.sha256(f"{UPSTREAM_REPO}\n{doc['title']}\n{body}".encode()).hexdigest()
        if not separator or tail or expected != digest:
            raise ValueError("Report changed; prepare it again")
        if doc.get("filed_url"):
            return _url(doc["filed_url"])
        matches = json.loads(
            _gh(
                "issue",
                "list",
                "--repo",
                UPSTREAM_REPO,
                "--state",
                "all",
                "--search",
                f"{digest} in:body",
                "--limit",
                "100",
                "--json",
                "url,body",
            )
        )
        existing = next(
            (item["url"] for item in matches if marker in (item.get("body") or "")), None
        )
        if existing:
            url = _url(existing)
        else:
            if doc.get("status") in ("submitting", "submission-unknown"):
                raise RuntimeError(
                    "Prior submission outcome is unknown; inspect GitHub before any retry"
                )
            doc["status"] = "submitting"
            doc["attempted_at"] = datetime.now(timezone.utc).isoformat()
            _save(path, doc)  # Fail before the external write if no receipt can be saved.
            try:
                url = _url(
                    _gh(
                        "issue",
                        "create",
                        "--repo",
                        UPSTREAM_REPO,
                        "--label",
                        "bug",
                        "--title",
                        doc["title"],
                        "--body-file",
                        "-",
                        body=doc["body"],
                    )
                )
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                doc["status"] = "submission-unknown"
                _save(path, doc)
                raise
        doc.update(status="filed", filed_url=url)
        _save(path, doc)
        try:
            record_decision(
                doc["analysis_dir"],
                kind="issue-filed",
                phase="issue",
                choice=url,
                why=doc["title"],
                inputs={"report": str(path)},
                source="agent",
            )
        except (OSError, ValueError):
            # The receipt is authoritative even when an unrelated journal is damaged.
            pass
        return url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Prepare a complete, shareable bug report")
    prepare.add_argument("--analysis-dir", type=Path, required=True)
    prepare.add_argument("--title", required=True)
    prepare.add_argument("--description", required=True)
    prepare.add_argument("--reproduction", required=True)
    prepare.add_argument("--expected-behavior", required=True)
    prepare.add_argument("--environment", required=True)
    prepare.add_argument("--context", required=True)
    prepare.add_argument("--proposed-fix", required=True)
    prepare.add_argument("--compatibility", required=True)
    prepare.add_argument("--verification", required=True)
    prepare.add_argument("--screenshots", default="Not applicable (command-line pipeline failure).")
    submit = commands.add_parser("open", help="Open the reviewed report and print its GitHub URL")
    submit.add_argument("--report", type=Path, required=True)
    args = vars(parser.parse_args())
    command = args.pop("command")
    try:
        if command == "prepare":
            print(prepare_report(**args))
        else:
            print(open_report(args["report"]))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"Issue not confirmed as filed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
