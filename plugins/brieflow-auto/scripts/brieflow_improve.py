#!/usr/bin/env python3
"""Prospective, local record -> candidate -> regression loop; never promotes a change.

The agent proposes edits. This helper freezes evidence, stages separate baseline and
candidate copies, and runs the same tests on both. Copies are NOT a security sandbox:
execute only trusted candidate tests under the host's normal execution permissions.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

RECORDS = (
    "run_manifest.json",
    "decisions.jsonl",
    "findings.json",
    "progress.json",
    "issue_draft.json",
    "issue_drafts.jsonl",
    "launch_review.json",
    "interview.json",
    "geometry.json",
)
# Small analysis-dir records that explain sweeps, memory and notebook stops.
RECORD_GLOBS = (
    ".brieflow/tuned_*.json",
    ".brieflow/*.notebook.log",
    ".brieflow/sweeps/**/*.tsv",
    ".brieflow/sweeps/**/*.log",
    ".brieflow/sweeps/**/winners.json",
    "slurm/mem_recommendations.json",
)
EFFICIENCY_GLOB = "logs/efficiency_*/efficiency_report*.csv"
PROTECTED = {
    "scripts/brieflow_qc.py",
    "scripts/brieflow_recover.py",
    "scripts/brieflow_launch_review.py",
    "scripts/brieflow_improve.py",
    "scripts/brieflow_notify.sh",
    "scripts/brieflow_trace.py",
    "scripts/brieflow_rollout_hook.py",
}
MAX_BYTES = 2_000_000
MAX_FILE_BYTES = 100_000_000
MAX_BUNDLE_BYTES = 200_000_000
EXCERPT_BYTES = 400_000
ANCESTOR_LEVELS = 3
# Transcripts and ledgers are split into chunks; pipeline logs are excerpted.
CHUNKED = {"optional-rollout", "development-ledger", "operator-log", "run-record"}
SBATCH_FLAGS = re.compile(
    r"(?<!\S)(?:-[pAtc]|--mem|--partition|--account|--time|--ntasks|--cpus-per-task|--gres)"
    r"[ =]\S+"
)
EXCERPT = re.compile(
    rb"error|exception|traceback|out_of_memory|oom|timeout|killed|sbatch call|invalid|failed", re.I
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def _chunks(data: bytes) -> list[bytes]:
    """Split on line boundaries into pieces of at most MAX_BYTES."""
    pieces, current = [], b""
    for line in data.splitlines(keepends=True):
        while len(line) > MAX_BYTES:
            if current:
                pieces.append(current)
                current = b""
            pieces.append(line[:MAX_BYTES])
            line = line[MAX_BYTES:]
        if len(current) + len(line) > MAX_BYTES:
            pieces.append(current)
            current = b""
        current += line
    return pieces + ([current] if current else [])


def _excerpt(path: Path) -> tuple[str, str]:
    """Distinct failure lines (digits normalized for de-duplication) and the file hash."""
    seen: dict[str, list] = {}
    size = 0
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            sha.update(raw)
            if not EXCERPT.search(raw):
                continue
            line = raw.decode("utf-8", "replace").rstrip()
            key = re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}|\d+", "#", line.strip())[:200]
            if key in seen:
                seen[key][1] += 1
            elif size < EXCERPT_BYTES:
                # Keep both ends of long lines, and the resource flags of a submission.
                text = line if len(line) <= 300 else f"{line[:150]} ... {line[-150:]}"
                if "sbatch" in line and len(line) > 300:
                    text += " [flags: " + " ".join(SBATCH_FLAGS.findall(line))[:200] + "]"
                seen[key] = [number, 1, text]
                size += len(seen[key][2]) + 20
    rows = [f"L{n} (x{count}) {text}" for n, count, text in seen.values()]
    return "\n".join(rows) + "\n", sha.hexdigest()


def _efficiency_summary(path: Path) -> str:
    """Per-rule job count, requested vs peak memory and CPU time from an efficiency CSV."""
    rules: dict[str, dict] = {}
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            rule = re.sub(r"_wildcards_.*", "", row.get("RuleName") or "?")
            entry = rules.setdefault(rule, {"jobs": 0, "req": set(), "rss": [], "cpu_s": 0.0})
            entry["jobs"] += 1
            try:
                entry["req"].add(round(float(row.get("RequestedMem_MB") or 0)))
                entry["rss"].append(float(row.get("MaxRSS_MB") or 0))
                entry["cpu_s"] += float(row.get("TotalCPU_sec") or 0)
            except ValueError:
                continue
    lines = ["rule\tjobs\trequested_mb\tmedian_peak_mb\tmax_peak_mb\tcpu_hours"]
    for rule, e in sorted(rules.items()):
        rss = sorted(e["rss"]) or [0.0]
        requested = ",".join(str(v) for v in sorted(e["req"]))
        lines.append(
            f"{rule}\t{e['jobs']}\t{requested}\t{rss[len(rss) // 2]:.0f}\t{rss[-1]:.0f}"
            f"\t{e['cpu_s'] / 3600:.2f}"
        )
    return "\n".join(lines) + "\n"


def _timestamps(content: str) -> list[str]:
    stamps = []
    for line in content.splitlines():
        try:
            stamp = json.loads(line).get("timestamp")
        except (ValueError, AttributeError):
            continue
        if isinstance(stamp, str):
            stamps.append(stamp)
    return stamps


def _iso(stamp: str | None) -> str | None:
    """Normalize trace (20260927T233036Z) and runtime ISO stamps to comparable UTC text."""
    if not stamp:
        return None
    if re.fullmatch(r"\d{8}T\d{6}Z", stamp):
        stamp = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:11]}:{stamp[11:13]}:{stamp[13:15]}+00:00"
    try:
        value = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _ancestors(analysis: Path) -> list[Path]:
    home = Path.home().resolve()
    found = []
    for folder in (analysis, *analysis.parents)[: ANCESTOR_LEVELS + 1]:
        if folder == home or folder == folder.parent:
            break
        found.append(folder)
    return found


def _superseded(snapshots: list[Path]) -> dict[Path, Path]:
    """Map each rollout snapshot that is a byte prefix of a longer one to that snapshot."""
    ordered = sorted(snapshots, key=lambda p: p.stat().st_size)
    result = {}
    for i, short in enumerate(ordered):
        head = short.read_bytes()
        for longer in reversed(ordered[i + 1 :]):
            with longer.open("rb") as stream:
                if stream.read(len(head)) == head:
                    result[short] = longer
                    break
    return result


def discover(analyses: list[Path]) -> dict[str, list[Path]]:
    """Traces, captured rollouts and *_LOG.md operator logs around each analysis dir."""
    found = {"traces": [], "rollouts": [], "operator_logs": []}
    for analysis in analyses:
        analysis = Path(analysis).resolve()
        found["traces"] += sorted(
            d for d in (analysis / ".brieflow" / "traces").glob("*") if (d / "trace.json").is_file()
        )
        for folder in _ancestors(analysis):
            found["rollouts"] += sorted(
                (folder / ".brieflow" / "rollouts").glob("*/rollout-*.jsonl")
            )
            found["operator_logs"] += sorted(p for p in folder.glob("*_LOG.md") if p.is_file())
    found["rollouts"] += [r for t in found["traces"] for r in sorted(t.glob("rollout-*.jsonl"))]
    return {key: list(dict.fromkeys(paths)) for key, paths in found.items()}


def _links(sources: list[dict], sessions: dict[str, str]) -> list[dict]:
    """Join runner traces to the session rollouts that cover them (session ID, else time)."""
    spans = {}
    for source in sources:
        if source["kind"] == "optional-rollout" and source.get("content"):
            sha = source.get("file_sha256", source["id"])
            stamps = sorted(filter(None, map(_iso, _timestamps(source["content"]))))
            if stamps:
                low, high = spans.get(sha, (stamps[0], stamps[-1]))
                spans[sha] = (min(low, stamps[0]), max(high, stamps[-1]))
    links = []
    for source in sources:
        if source["kind"] != "trace-metadata" or not source.get("content"):
            continue
        trace = json.loads(source["content"])
        started = _iso(trace.get("started_at"))
        for sha, (low, high) in spans.items():
            if trace.get("agent_session_id") and trace["agent_session_id"] == sessions.get(sha):
                method = "session-id"
            elif started and low <= started <= high:
                method = "time-window"
            else:
                continue
            links.append({"trace": source["path"], "rollout_sha256": sha, "method": method})
    return links


def collect(
    analyses: list[Path],
    output: Path,
    ledgers: list[Path] = (),
    rollouts: list[Path] = (),
    traces: list[Path] = (),
    *,
    auto_discover: bool = True,
) -> Path:
    """Freeze LOCAL evidence: explicit selections plus records found next to each analysis dir.

    Over-limit files are chunked (transcripts, ledgers, records) or excerpted (pipeline logs);
    anything not retained is listed under `skipped`, never silently dropped.
    """
    if output.exists():
        raise ValueError("Choose a new evidence bundle; do not overwrite an earlier round")
    analyses = [Path(a) for a in analyses]
    found = discover(analyses) if auto_discover else {}
    traces = list(dict.fromkeys([*map(Path, traces), *found.get("traces", [])]))
    rollouts = list(dict.fromkeys([*map(Path, rollouts), *found.get("rollouts", [])]))
    paths = [(a / ".brieflow" / name, "run-record") for a in analyses for name in RECORDS]
    for analysis in analyses:
        paths += [
            (p, "run-record")
            for pattern in RECORD_GLOBS
            for p in sorted(analysis.glob(pattern))
            if "__marimo__" not in p.parts and p.is_file()
        ]
        logs = sorted((analysis / "logs").glob("*.log"))
        paths += [(p, "log-excerpt") for p in logs if p.is_file()]
        paths += [(p, "efficiency-summary") for p in sorted(analysis.glob(EFFICIENCY_GLOB))]
    paths += [(Path(p), "development-ledger") for p in ledgers]
    paths += [(p, "operator-log") for p in found.get("operator_logs", []) if p not in ledgers]
    paths += [(Path(p), "optional-rollout") for p in rollouts]
    for trace in traces:
        paths += [(trace / "trace.json", "trace-metadata"), (trace / "runner.log", "run-record")]
        paths += [
            (trace / stage / name, "run-record")
            for stage in ("before", "after")
            for name in RECORDS
        ]
    superseded = _superseded([p for p, kind in paths if kind == "optional-rollout" and p.is_file()])
    sources, skipped, seen, sessions, total = [], [], {}, {}, 0
    for path, kind in dict.fromkeys(paths):
        if not path.exists():
            if kind in ("run-record", "log-excerpt", "efficiency-summary", "trace-metadata"):
                continue
            raise ValueError(f"Selected evidence does not exist: {path}")
        size = path.stat().st_size
        record = {"path": str(path.resolve()), "kind": kind, "bytes": size}
        if path.is_symlink() or not path.is_file():
            skipped.append({**record, "reason": "not a regular file"})
            continue
        if path in superseded:
            skipped.append({**record, "reason": f"byte prefix of {superseded[path].name}"})
            continue
        if kind == "optional-rollout":
            index = path.parent / "trace.json"
            if index.is_file():
                for item in json.loads(index.read_text()).get("rollouts", []):
                    sessions[item.get("sha256")] = item.get("session_id")
        if kind == "log-excerpt" and size <= MAX_BYTES:
            kind = record["kind"] = "run-record"
        if kind == "efficiency-summary":
            summary = _efficiency_summary(path).encode()
            pieces = [(summary, {"summary_of": digest(path.read_bytes())})]
        elif kind == "log-excerpt" or (size > MAX_BYTES and kind not in CHUNKED):
            text, file_sha = _excerpt(path)
            record["kind"] = "log-excerpt"
            pattern = EXCERPT.pattern.decode()
            pieces = [(text.encode(), {"excerpt_of": file_sha, "pattern": pattern})]
        elif size > MAX_FILE_BYTES:
            skipped.append({**record, "reason": f"over {MAX_FILE_BYTES} bytes"})
            continue
        else:
            data = path.read_bytes()
            parts = _chunks(data) if len(data) > MAX_BYTES else [data]
            whole = kind == "optional-rollout" or len(parts) > 1
            extra = {"file_sha256": digest(data)} if whole else {}
            pieces = [
                (part, {**extra, "chunk": [i + 1, len(parts)]} if len(parts) > 1 else extra)
                for i, part in enumerate(parts)
            ]
        if total + sum(len(part) for part, _ in pieces) > MAX_BUNDLE_BYTES:
            skipped.append({**record, "reason": f"bundle over {MAX_BUNDLE_BYTES} bytes"})
            continue
        for part, extra in pieces:
            source = {"id": digest(part), **record, **extra, "bytes": len(part)}
            if source["id"] in seen:
                source["duplicate_of"] = seen[source["id"]]
            else:
                seen[source["id"]] = source["path"]
                source["content"] = part.decode("utf-8", "replace")
                total += len(part)
            sources.append(source)
    if not sources:
        raise ValueError("No evidence found")
    output.parent.mkdir(parents=True, exist_ok=True)
    save(
        output,
        {
            "schema": "brieflow-improvement-evidence/v2",
            "sources": sources,
            "skipped": skipped,
            "links": _links(sources, sessions),
            "auto_discovered": auto_discover,
            "rollouts_included": any(
                s["kind"] == "optional-rollout" and "content" in s for s in sources
            ),
            "public_release": False,
        },
    )
    return output


def _relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or len(path.parts) < 2:
        raise ValueError("Candidate edits must stay inside the plugin")
    if path.parts[0] not in ("scripts", "skills", "commands", "tests"):
        raise ValueError("Candidate cannot change data policies, packaging, or external pipelines")
    if path.as_posix() in PROTECTED or path.name == "conftest.py":
        raise ValueError(
            "Approval, QC, recovery, evaluation, and notification policies are protected"
        )
    if path.suffix not in (".py", ".md"):
        raise ValueError("Only Python helpers/tests and Markdown instructions are supported")
    return path


def _snapshot(source: Path, destination: Path) -> None:
    def ignore(folder, names):
        excluded = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv"}
        return [n for n in names if n in excluded or n.endswith(".egg-info")]

    shutil.copytree(source, destination, ignore=ignore, symlinks=True)
    if any(p.is_symlink() for p in destination.rglob("*")):
        raise ValueError("Snapshot contains symlinks; use a self-contained source checkout")


def stage(source: Path, bundle: Path, proposal: Path, workspace: Path) -> Path:
    """Apply at most ten hash-pinned edits to a separate candidate, not live code."""
    source = source.resolve()
    if workspace.exists() or workspace.resolve().is_relative_to(source):
        raise ValueError("Use a new workspace outside the source plugin")
    evidence = json.loads(bundle.read_text())
    spec = json.loads(proposal.read_text())
    for field in ("hypothesis", "compatibility", "held_out_plan"):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise ValueError(f"Proposal requires {field}")
    known = {item["id"] for item in evidence["sources"]}
    if not spec.get("evidence_ids") or not set(spec["evidence_ids"]).issubset(known):
        raise ValueError("Proposal must reference evidence in this bundle")
    edits = spec.get("edits", [])
    if not 1 <= len(edits) <= 10:
        raise ValueError("One candidate round permits 1–10 edits")
    seen = set()
    regression_tests = []
    for edit in edits:
        rel = _relative(edit["path"])
        if rel in seen:
            raise ValueError("Duplicate edit path")
        seen.add(rel)
        original = source / rel
        if original.is_symlink() or any(
            p.is_symlink() for p in original.parents if p != source.parent
        ):
            raise ValueError("Symlink edit target")
        actual = digest(original.read_bytes()) if original.exists() else None
        if actual != edit.get("before_sha256"):
            raise ValueError(f"Source changed: {rel}")
        if not isinstance(edit.get("content"), str) or len(edit["content"].encode()) > MAX_BYTES:
            raise ValueError("Candidate content missing or too large")
        if rel.parts[0] == "tests":
            if original.exists() or not rel.name.startswith("test_"):
                raise ValueError("Existing tests are frozen; add a new regression test")
            regression_tests.append(rel)
    if not regression_tests:
        raise ValueError("A new regression test is required")
    workspace.mkdir(parents=True)
    for variant in ("baseline", "candidate"):
        root = workspace / variant
        plugin = root / "plugins" / "brieflow-auto"
        _snapshot(source, plugin)
        # Preserve the docs/packaging checks' repo-relative context.
        for name in (
            "README.md",
            "HARNESS.md",
            "CLAUDE.md",
            "CHANGELOG.md",
            ".claude-plugin/marketplace.json",
        ):
            original = source.parent.parent / name
            if original.is_file() and not original.is_symlink():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
        for edit in edits:
            rel = Path(edit["path"])
            if variant == "candidate" or rel in regression_tests:
                target = plugin / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(edit["content"])
    shutil.copy2(bundle, workspace / "evidence.json")
    shutil.copy2(proposal, workspace / "proposal.json")
    hashes = {}
    for variant in ("baseline", "candidate"):
        hashes[variant] = _tree_hash(workspace / variant)
    save(
        workspace / "round.json",
        {
            "schema": "brieflow-improvement-round/v1",
            "status": "staged",
            "source": str(source),
            "tree_hashes": hashes,
            "proposal_sha256": digest(proposal.read_bytes()),
        },
    )
    return workspace


def _tree_hash(root: Path) -> str:
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not {"__pycache__", ".pytest_cache", ".ruff_cache"}.intersection(
            path.parts
        ):
            records.append([str(path.relative_to(root)), digest(path.read_bytes())])
    return digest(json.dumps(records).encode())


def evaluate(workspace: Path, *, execute_tests: bool = False, timeout: int = 300) -> dict:
    if not execute_tests:
        raise ValueError(
            "Test execution requires --execute-tests in an authorized execution environment"
        )
    round_doc = json.loads((workspace / "round.json").read_text())
    if (workspace / "evaluation.json").exists():
        raise ValueError("Evaluation already recorded; stage another round to retry")
    if not 1 <= timeout <= 1800:
        raise ValueError("Timeout must be 1–1800 seconds per test suite")
    results = {}
    for variant in ("baseline", "candidate"):
        root = workspace / variant
        if _tree_hash(root) != round_doc["tree_hashes"][variant]:
            raise ValueError(f"Frozen {variant} changed; stage another round")
        env = dict(
            os.environ,
            BRIEFLOW_NOTIFY_MODE="disabled",
            BRIEFLOW_NOTIFY_ENV_FILE="",
            BRIEFLOW_SLACK_WEBHOOK="",
            PYTHONDONTWRITEBYTECODE="1",
        )
        # Do not pass API credentials to candidate tests. Filesystem/network isolation
        # must still be provided by the caller's sandbox, not by this environment filter.
        env = {
            k: v for k, v in env.items() if not any(s in k for s in ("TOKEN", "API_KEY", "SECRET"))
        }
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "tests", "-q"],
                cwd=root / "plugins" / "brieflow-auto",
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            results[variant] = {"returncode": result.returncode}
            (workspace / f"{variant}.log").write_text(result.stdout + result.stderr)
        except subprocess.TimeoutExpired:
            results[variant] = {"returncode": None, "timeout": True}
    base = results["baseline"]["returncode"]
    candidate = results["candidate"]["returncode"]
    if base is None or candidate is None or base not in (0, 1) or candidate not in (0, 1):
        status = "inconclusive"
    elif candidate != 0:
        status = "rejected"
    elif base == 0:
        status = "no-regression-fix-demonstrated"
    else:
        status = "review-required"
    result = {
        "status": status,
        "tests": results,
        "promoted": False,
        "held_out_evaluation": "not performed by this helper",
        "proposal_sha256": round_doc["proposal_sha256"],
    }
    save(workspace / "evaluation.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect_cmd = sub.add_parser("collect")
    collect_cmd.add_argument("--analysis-dir", action="append", type=Path, default=[])
    collect_cmd.add_argument("--ledger", action="append", type=Path, default=[])
    collect_cmd.add_argument("--rollout", action="append", type=Path, default=[])
    collect_cmd.add_argument("--trace-dir", action="append", type=Path, default=[])
    collect_cmd.add_argument("--output", type=Path, required=True)
    collect_cmd.add_argument(
        "--no-discover", action="store_true", help="Use only the explicitly selected evidence"
    )
    stage_cmd = sub.add_parser("stage")
    stage_cmd.add_argument("--source-plugin", type=Path, required=True)
    stage_cmd.add_argument("--bundle", type=Path, required=True)
    stage_cmd.add_argument("--proposal", type=Path, required=True)
    stage_cmd.add_argument("--workspace", type=Path, required=True)
    evaluate_cmd = sub.add_parser("evaluate")
    evaluate_cmd.add_argument("--workspace", type=Path, required=True)
    evaluate_cmd.add_argument("--execute-tests", action="store_true")
    evaluate_cmd.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    try:
        if args.command == "collect":
            print(
                collect(
                    args.analysis_dir,
                    args.output,
                    args.ledger,
                    args.rollout,
                    args.trace_dir,
                    auto_discover=not args.no_discover,
                )
            )
        elif args.command == "stage":
            print(stage(args.source_plugin, args.bundle, args.proposal, args.workspace))
        else:
            print(
                json.dumps(
                    evaluate(args.workspace, execute_tests=args.execute_tests, timeout=args.timeout)
                )
            )
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Improvement round stopped: {exc}\n")


if __name__ == "__main__":
    main()
