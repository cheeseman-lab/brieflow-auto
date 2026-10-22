"""Docs name flags, paths and versions that exist (spec §6).

Failing here means a skill, command, README, CLAUDE.md or HARNESS.md drifted from
the code — fix the doc or the code, never the test's allowlist, unless the flag
belongs to an external tool (then add it to EXTERNAL_FLAGS with the tool named).

Scope of the flag check: a doc line is a claim about *our* flags only when it names
one of our executables, or when it invokes no external tool at all (a bare `--only
sbs` in prose). A line that drives gcloud, conda, snakemake, sbatch or flow.sh is
documenting that tool's surface, not ours, so its flags are not checked — that is
what keeps EXTERNAL_FLAGS a short list of flags the docs mention on their own
rather than a mirror of every external CLI.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parent.parent
REPO = PLUGIN.parent.parent
DOC_FILES = sorted([*PLUGIN.glob("skills/**/*.md"), *PLUGIN.glob("commands/*.md")])
ROOT_DOCS = [p for p in (REPO / "README.md", REPO / "CLAUDE.md", REPO / "HARNESS.md") if p.exists()]

# Flags the docs name on their own (not on one of our command lines), each owned by
# an external tool. Every entry is required by a real doc line; keep it that way.
EXTERNAL_FLAGS = {
    "--batch",  # snakemake
    "--notemp",  # snakemake
    "--executor",  # snakemake
    "--until",  # snakemake
    "--rerun-triggers",  # snakemake
    "--slurm-array-limit",  # snakemake-executor-plugin-slurm
    "--mail-user",  # sbatch
    "--time",  # sbatch
    "--solver",  # conda
    "--project",  # gcloud
    "--zone",  # gcloud
    "--listen-on-stdin",  # gcloud compute start-iap-tunnel
    "--tunnel-through-iap",  # gcloud compute ssh
    "--command",  # gcloud compute ssh --command (wraps one of our commands)
    "--quiet",  # gcloud (non-interactive)
    "--snapshot-type",  # gcloud compute snapshots create
    "--repo",  # gh issue create
    "--version",  # python --version
}
# Commands whose flags belong to somebody else. flow.sh is the analysis repo's
# snakemake wrapper — brieflow's surface, not the plugin's.
EXTERNAL_COMMANDS = (
    "gcloud",
    "gsutil",
    "gcsfuse",
    "sbatch",
    "srun",
    "squeue",
    "sacct",
    "sacctmgr",
    "scancel",
    "scontrol",
    "sinfo",
    "snakemake",
    "flow.sh",
    "conda",
    "mamba",
    "pip",
    "uv",
    "git",
    "gh",
    "ssh",
    "scp",
    "rsync",
    "tmux",
    "curl",
    "wget",
    "systemd-run",
    "systemctl",
    "journalctl",
    "marimo",
    "jupyter",
    "papermill",
    "ipykernel",
    "apt-get",
    "apt",
    "diff",
    "docker",
    "module",
)
TOKEN_DENYLIST = (
    "wi.mit.edu",
    "broadinstitute",
    "matteodibernardo",
    "mdiberna",
    "/data/ops_analysis",
)
# Site paths that must not appear anywhere in the tracked tree (the repository is public).
PRIVATE_PATH_TOKENS = ("/lab/", "/archive/cheeseman", "cheeseman_scratch")
# sha256 of lowercase words that name unpublished screens, legacy screen aliases or
# collaborator infrastructure; hashed so this list does not publish them itself.
PRIVATE_WORD_SHA256 = frozenset({
    "cee515720c19b3ad855d35ab72eddd9107bde3496c8d9b0603293edc8b66d11b",
    "6d0281faf0b473fc91514eaa7198d40bf627f58d0cba8baa2fa57687e57f9b60",
    "a4261182773773d15e130df75edf58d895f1f1bb005604f6e08d5432054b57a6",
    "eaa80b541be67b8adaf9cd749f00ba16e9122e99f90feebb5bda8e8f9b69d45c",
    "a8d92ff55b76e0bf5e39b0afa8a139ece43e420b6e56af16ab9ae14b576a2b5f",
    "530c4fc0e55038578184991c3f991043d4ec5bdceae63ed8f80743d313fb382d",
    "84abd1909aeb15e1d84bbee98c583c569366f5c006e397086028e3d6da12d2bb",
    "e27622e8321da74e6eafb1cee8e7804200cb536ef606173c0376d5f8c470ceaf",
    "474c094320c5498b05ba2a564d07d1d8cb6635dcdb5a006f554dc61fcd15e7a4",
    "eb75e294604215dd267297f28da3678b144e27dfb83e9f62dda4a67904c502b1",
    "e300a70f37729cf967a32d2229aec7ed5a29eb1022c4138b708acf645a9daa27",
    "b9564d466fc2865f2a36c18e3a6a03af23ce926680d8a20c2d512ac4c1ab6e0f",
})
PATH_RE = re.compile(r"(?<![\w/])((?:scripts|data|commands|skills|cloud|bin)/[A-Za-z0-9_./\-]+)")
FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]+)")
CODE_RE = re.compile(r"```.*?```|`[^`\n]+`", re.S)
EXTERNAL_CMD_RE = re.compile(
    r"(?<![\w/.\-])(" + "|".join(re.escape(c) for c in EXTERNAL_COMMANDS) + r")(?![\w\-])"
)
# argparse declares a flag inline or on its own line after `add_argument(`, possibly
# behind a short alias: add_argument("-n", "--name", ...).
ADD_ARG_RE = re.compile(r'add_argument\(\s*(?:"-[A-Za-z]"\s*,\s*)*"(--[a-z][a-z0-9-]+)"')
# A flag string followed by an argparse keyword — catches declarations built outside
# a literal add_argument( call (shared option tables, helper adders).
KWARG_FLAG_RE = re.compile(
    r'"(--[a-z][a-z0-9-]+)"\s*,\s*'
    r"(?:action|dest|default|type|choices|help|required|nargs|metavar)\s*="
)
CASE_ARM_RE = re.compile(r"(--[a-z][a-z0-9-]+)\)")  # shell case arm


SKIP_PARTS = {"_test_screens", "tests", "__pycache__", ".pytest_cache", ".ruff_cache"}
TEXT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".json"}


def _scanned_files():
    """Every shipped file the whole-file checks read: plugin text files (plus the
    extension-less `bin/` entry points), the repo-root docs and the marketplace
    manifest (a doc of record for what the plugin advertises)."""
    out = []
    for p in PLUGIN.rglob("*"):
        if not p.is_file() or SKIP_PARTS & set(p.parts) or ".egg-info" in str(p):
            continue
        if p.suffix in TEXT_SUFFIXES or p.parent == PLUGIN / "bin":
            out.append(p)
    out += REPO.glob("*.md")
    out += REPO.glob(".claude-plugin/*.json")
    return sorted(out, key=str)


SCANNED_FILES = _scanned_files()


def _rel(p: Path) -> str:
    return str(p.relative_to(REPO))


def _our_executables():
    """Basenames of everything in this repo a doc can legitimately invoke."""
    names = set()
    for pat in ("scripts/**/*.py", "scripts/**/*.sh", "cloud/*.sh", "bin/*"):
        names |= {p.name for p in PLUGIN.glob(pat) if p.is_file()}
    names |= {p.name for p in (REPO / "scripts").glob("*.sh")}
    return names


OUR_CMD_RE = re.compile(
    "|".join(re.escape(n) for n in sorted(_our_executables(), key=len, reverse=True))
)


def _code_spans(text: str):
    for m in CODE_RE.finditer(text):
        yield m.group(0)


def _logical_lines(span: str):
    """Yield shell-logical lines: continuations (`\\` at EOL) joined into one."""
    buf = ""
    for raw in span.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        yield buf + line
        buf = ""
    if buf:
        yield buf


def _argparse_flags():
    flags = set()
    for py in PLUGIN.glob("scripts/**/*.py"):
        src = py.read_text()
        flags |= set(ADD_ARG_RE.findall(src))
        flags |= set(KWARG_FLAG_RE.findall(src))
    shells = [
        *PLUGIN.glob("scripts/**/*.sh"),
        *PLUGIN.glob("cloud/*.sh"),
        *PLUGIN.glob("bin/*"),
        *(REPO / "scripts").glob("*.sh"),
    ]
    for sh in shells:
        if sh.is_file():
            flags |= set(CASE_ARM_RE.findall(sh.read_text(errors="ignore")))
    return flags


KNOWN_FLAGS = _argparse_flags() | EXTERNAL_FLAGS


@pytest.mark.parametrize("doc", DOC_FILES + ROOT_DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_flags_named_in_docs_exist(doc):
    unknown = set()
    for span in _code_spans(doc.read_text()):
        for line in _logical_lines(span):
            named = set(FLAG_RE.findall(line)) - KNOWN_FLAGS
            if not named:
                continue
            if OUR_CMD_RE.search(line) or not EXTERNAL_CMD_RE.search(line):
                unknown |= named
    assert not unknown, f"{doc.relative_to(REPO)} names flags no script defines: {sorted(unknown)}"


@pytest.mark.parametrize("doc", DOC_FILES + ROOT_DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_paths_named_in_docs_exist(doc):
    missing = []
    for span in _code_spans(doc.read_text()):
        for match in PATH_RE.finditer(span):
            rel = match.group(1).rstrip(".,:)")
            # A placeholder or glob either sits inside the match or truncates it
            # (`scripts/brieflow_*.py` matches only up to the `*`), so check both.
            if any(c in rel for c in "<*{") or span[match.end() : match.end() + 1] in (
                "<",
                "*",
                "{",
            ):
                continue
            # Plugin docs write plugin-relative paths, root docs repo-relative ones.
            if not ((PLUGIN / rel).exists() or (REPO / rel).exists()):
                missing.append(rel)
    assert not sorted(set(missing)), (
        f"{doc.relative_to(REPO)} names paths that do not exist: {sorted(set(missing))}"
    )


def test_versions_agree_and_claude_md_has_no_current_marker():
    v_plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["version"]
    v_py = re.search(
        r'^version\s*=\s*"([^"]+)"', (PLUGIN / "pyproject.toml").read_text(), re.M
    ).group(1)
    v_mkt = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    mkt_versions = set(re.findall(r'"version":\s*"([^"]+)"', json.dumps(v_mkt)))
    assert {v_plugin, v_py} | mkt_versions == {v_plugin}, (v_plugin, v_py, mkt_versions)
    assert "(current)" not in (REPO / "CLAUDE.md").read_text()


def test_py_modules_lists_every_script_module():
    """`py-modules` is the installed view of `scripts/`. A module added or deleted
    there and not mirrored here is missing from an editable install, so it imports
    only for the callers that happen to have `scripts/` on sys.path already."""
    block = re.search(r"py-modules = \[(.*?)\]", (PLUGIN / "pyproject.toml").read_text(), re.S)
    declared = re.findall(r'"([^"]+)"', block.group(1))
    assert declared == sorted(declared), "py-modules is not sorted"
    assert set(declared) == {p.stem for p in (PLUGIN / "scripts").glob("*.py")}


@pytest.mark.parametrize("path", SCANNED_FILES, ids=_rel)
def test_no_operator_specific_tokens(path):
    text = path.read_text(errors="ignore")
    # A fenced block whose first line is `# example` is a labelled example — allowed.
    text = re.sub(r"```[^\n]*\n# example\n.*?```", "", text, flags=re.S)
    hits = [t for t in TOKEN_DENYLIST if t in text]
    assert not hits, f"{_rel(path)} contains operator-specific tokens {hits}"


def test_no_retired_names():
    bad = []
    for p in SCANNED_FILES:
        t = p.read_text(errors="ignore")
        for tok in (
            "brieflow-migrate",
            "PLUGIN_SCRIPTS",
            "latency-wait 60",
            "brieflow_run_screen",
            "brieflow-run-screen",
        ):
            if tok in t:
                bad.append(f"{_rel(p)}: {tok}")
        # The changelog records the retired name by design; everything else renames.
        if re.search(r"autopilot", t, re.I) and p.name != "CHANGELOG.md":
            for line in t.splitlines():
                if (
                    re.search(r"autopilot", line, re.I)
                    and "LEGACY_PROGRESS_NAME" not in line
                ):
                    bad.append(f"{_rel(p)}: {line.strip()[:80]}")
    assert not bad, "retired names still present:\n" + "\n".join(sorted(bad))


def _tracked_text_files():
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    files = []
    for rel in out.splitlines():
        p = REPO / rel
        if p.is_file() and p.resolve() != Path(__file__).resolve():
            files.append(p)
    return files


def test_tracked_tree_has_no_private_tokens():
    hits = []
    for p in _tracked_text_files():
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        found = [t for t in TOKEN_DENYLIST + PRIVATE_PATH_TOKENS if t in text]
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        if any(hashlib.sha256(w.encode()).hexdigest() in PRIVATE_WORD_SHA256 for w in words):
            found.append("<private screen or collaborator name>")
        if found:
            hits.append(f"{p.relative_to(REPO)}: {found}")
    assert not hits, "\n".join(hits)
