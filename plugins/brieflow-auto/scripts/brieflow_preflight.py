#!/usr/bin/env python3
"""
brieflow_preflight.py — Static config validator for brieflow analyses.

Walks the brieflow submodule's `workflow/rules/*.smk` and
`workflow/scripts/**/*.py`, derives:
  - required config keys (every `config[K]`, `params.config.get(K)`, and
    `snakemake.params.K` lookup),
  - accepted-value surfaces (every `if X == "Y": ... elif X == "Z": ...
    else: raise ValueError("Unknown ...")` pattern),
  - rules without explicit `resources:` blocks (OOM-risk at well/full scale
    because they inherit the global default mem_mb=3000),

then validates the analysis config against those surfaces. Catches
config↔rule-code mismatches (e.g. `cp_method: cp_multichannel` when the
script only accepts `cp_measure` or `cp_emulator`) before submitting any
slurm jobs.

Exits nonzero if any HIGH severity issues are found. /brieflow-module should
run this as a gate before launching flow.sh.

Usage:
    brieflow_preflight.py --analysis-dir <path>
    brieflow_preflight.py --analysis-dir <path> --json
    brieflow_preflight.py --analysis-dir <path> --brieflow-dir <path>

The --analysis-dir is the screen analysis root containing config/, brieflow/
(submodule), etc. --brieflow-dir overrides if the submodule is elsewhere.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Severity & Issue
# ---------------------------------------------------------------------------


SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    file: str = ""
    line: int = 0
    suggestion: str = ""

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "suggestion": self.suggestion,
        }


# ---------------------------------------------------------------------------
# AST visitors for python scripts
# ---------------------------------------------------------------------------


class _ConfigKeyVisitor(ast.NodeVisitor):
    """Collects `config[...]`, `config.get(...)`, `snakemake.params.X`,
    `snakemake.config[...]` and similar patterns that imply config keys."""

    def __init__(self):
        self.keys_required: set[str] = set()
        self.keys_optional: set[str] = set()
        self.params_used: set[str] = set()

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # config["X"] / snakemake.config["X"]
        if isinstance(node.value, ast.Name) and node.value.id == "config":
            key = self._const_str(node.slice)
            if key:
                self.keys_required.add(key)
        elif (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "snakemake"
            and node.value.attr == "config"
        ):
            key = self._const_str(node.slice)
            if key:
                self.keys_required.add(key)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # config.get("X", default) / snakemake.config.get("X")
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            base = node.func.value
            if (isinstance(base, ast.Name) and base.id == "config") or (
                isinstance(base, ast.Attribute)
                and isinstance(base.value, ast.Name)
                and base.value.id == "snakemake"
                and base.attr == "config"
            ):
                if node.args:
                    key = self._const_str(node.args[0])
                    if key:
                        if len(node.args) >= 2:
                            self.keys_optional.add(key)
                        else:
                            self.keys_required.add(key)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # snakemake.params.X
        if (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "snakemake"
            and node.value.attr == "params"
        ):
            self.params_used.add(node.attr)
        self.generic_visit(node)

    @staticmethod
    def _const_str(node) -> str | None:
        """Return the literal string from an ast.Constant or older ast.Str."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if hasattr(ast, "Index") and isinstance(node, ast.Index):
            return _ConfigKeyVisitor._const_str(node.value)
        return None


_UNKNOWN_VALUE_RE = re.compile(
    # Match the leading literal `Unknown <varname>:` and then capture the rest
    # of the f-string up to its closing quote (handles multi-line, both quote
    # styles, and the variant `Choose 'a' or 'b'`).
    # Anchored on `raise ValueError(` and the literal opening that follows.
    r"raise\s+ValueError\(\s*f?(?P<q>[\"'])Unknown\s+(?P<var>\w+)\s*:"
    r"(?P<body>.*?)(?P=q)\s*\)",
    re.DOTALL,
)

# Inside the body, options appear in single-quoted literals e.g. 'cp_measure'.
# We use a tight regex so we don't pick up the f-string interpolation `{var}`
# or other quoted strings that aren't options.
_OPTION_RE = re.compile(r"'([^']+)'")


def parse_unknown_value_guards(source: str, file_path: str) -> list[dict]:
    """Find `raise ValueError("Unknown X: {X}. Choose 'a' or 'b'.")` patterns
    and extract the variable name + accepted value list.
    """
    guards: list[dict] = []
    for m in _UNKNOWN_VALUE_RE.finditer(source):
        var = m.group("var")
        body = m.group("body")
        # Look for explicit "Choose 'X' or 'Y'" / "Choose one of ['X', 'Y']" enumerations
        choose_idx = body.lower().find("choose")
        if choose_idx >= 0:
            options_blob = body[choose_idx:]
        else:
            options_blob = body
        options = _OPTION_RE.findall(options_blob)
        # Drop options that are obviously interpolation tokens or empty.
        options = [o for o in options if o and "{" not in o]
        if options:
            line_no = source[: m.start()].count("\n") + 1
            guards.append(
                {
                    "key": var,
                    "accepted": sorted(set(options)),
                    "file": file_path,
                    "line": line_no,
                }
            )
    return guards


# Second guard shape (introduced by combinatorial-SBS's call_reads.py): a
# `supported = {...}` dict literal keyed by one variable's accepted values,
# whose entries are themselves the accepted values for a second variable —
# followed by a message of the form:
#   raise ValueError(f"X '{method}' is not supported for Y '{chemistry}'. "
#                     f"Use one of {supported[chemistry]}.")
# Unlike the flat `Unknown X` guard, the accepted set for `method` depends on
# the current value of `chemistry`, so the guard carries both the flat union
# (fallback) and the per-dependency-value map.
_SUPPORTED_DICT_RE = re.compile(
    r"(?P<dictvar>\w+)\s*=\s*\{\s*\n"
    r"(?P<body>(?:\s*[\"'][^\"']+[\"']\s*:\s*\([^)]*\)\s*,?\s*\n)+)"
    r"\s*\}",
)
_SUPPORTED_PAIR_RE = re.compile(r"[\"'](?P<k>[^\"']+)[\"']\s*:\s*\((?P<vals>[^)]*)\)")
# Dict-literal tuple values may use either quote style (unlike the message-body
# options _OPTION_RE targets, which are always single-quoted inside an
# f-string), so match both here.
_SUPPORTED_OPTION_RE = re.compile(r"[\"']([^\"']+)[\"']")
_SUPPORTED_RAISE_RE = re.compile(r"raise\s+ValueError\((?P<msg>.*?)\)\s*(?:\n|$)", re.DOTALL)
_SUPPORTED_FIELD_RE = re.compile(r"'\{(?P<field>\w+)\}'\s+is not supported")

# How far past a `supported = {...}` dict to look for its matching
# `raise ValueError(... Use one of {dictvar[depvar]} ...)`.
_SUPPORTED_LOOKAHEAD_CHARS = 800


def parse_supported_dict_guards(source: str, file_path: str) -> list[dict]:
    """Find `<dict> = {"a": (...), "b": (...)}` + a nearby
    `raise ValueError(f"... '{field}' is not supported for ... '{dep}'. "
    f"Use one of {{<dict>[<dep>]}}.")` pair, and extract a conditional
    accepted-value guard: `field`'s accepted set depends on `dep`'s value.
    """
    guards: list[dict] = []
    for m in _SUPPORTED_DICT_RE.finditer(source):
        dictvar = m.group("dictvar")
        body = m.group("body")
        after = source[m.end() : m.end() + _SUPPORTED_LOOKAHEAD_CHARS]

        dep_var = None
        field_var = None
        use_re = re.compile(r"Use one of \{" + re.escape(dictvar) + r"\[(\w+)\]\}")
        for rm in _SUPPORTED_RAISE_RE.finditer(after):
            msg = rm.group("msg")
            use_m = use_re.search(msg)
            if not use_m:
                continue
            dep_var = use_m.group(1)
            field_m = _SUPPORTED_FIELD_RE.search(msg)
            if field_m:
                field_var = field_m.group("field")
            break

        if not dep_var or not field_var:
            continue

        accepted_by_dependency: dict[str, list[str]] = {}
        for pm in _SUPPORTED_PAIR_RE.finditer(body):
            options = _SUPPORTED_OPTION_RE.findall(pm.group("vals"))
            if options:
                accepted_by_dependency[pm.group("k")] = sorted(set(options))
        if not accepted_by_dependency:
            continue

        flat_union = sorted({v for opts in accepted_by_dependency.values() for v in opts})
        line_no = source[: m.start()].count("\n") + 1
        guards.append(
            {
                "key": field_var,
                "accepted": flat_union,
                "depends_on": dep_var,
                "accepted_by_dependency": accepted_by_dependency,
                "file": file_path,
                "line": line_no,
            }
        )
    return guards


# ---------------------------------------------------------------------------
# .smk parsing — find rules without `resources:` blocks
# ---------------------------------------------------------------------------


_RULE_HEADER_RE = re.compile(r"^rule\s+(\w+)\s*:\s*$", re.MULTILINE)


def find_rules_without_resources(smk_path: Path) -> list[dict]:
    """Find every `rule X:` block in a .smk file and report whether it has
    a `resources:` directive with `mem_mb=`.
    """
    try:
        text = smk_path.read_text()
    except OSError:
        return []

    rules: list[dict] = []
    rule_starts = [
        (m.start(), m.group(1), text[: m.start()].count("\n") + 1)
        for m in _RULE_HEADER_RE.finditer(text)
    ]

    for i, (start, name, line) in enumerate(rule_starts):
        end = rule_starts[i + 1][0] if i + 1 < len(rule_starts) else len(text)
        block = text[start:end]
        has_resources = bool(re.search(r"^\s+resources\s*:", block, re.MULTILINE))
        has_mem_mb = bool(re.search(r"\bmem_mb\s*=", block))
        rules.append(
            {
                "rule": name,
                "file": str(smk_path),
                "line": line,
                "has_resources_block": has_resources,
                "has_mem_mb": has_mem_mb,
            }
        )
    return rules


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


@dataclass
class BrieflowSurface:
    required_keys: set[str] = field(default_factory=set)
    optional_keys: set[str] = field(default_factory=set)
    params_used: set[str] = field(default_factory=set)
    accepted_value_guards: list[dict] = field(default_factory=list)
    rules_without_resources: list[dict] = field(default_factory=list)
    n_smk_files: int = 0
    n_py_files: int = 0
    n_lib_py_files: int = 0


# workflow/lib guards that enumerate an *internal* assertion rather than a
# config-facing value: the flagged variable is a plain function parameter
# with no caller in workflow/scripts or workflow/rules (notebook-only, no
# config[...]/snakemake.params surface), so there is no config key preflight
# could ever legitimately check it against. Keyed by (path suffix, guard
# key) so a same-named guard elsewhere in lib isn't accidentally suppressed.
# Audited 2026-08-04 against brieflow submodule @ f648fb8.
# segment_secondary_object.estimate_second_obj_diameter added 2026-09-09: a
# notebook-only helper with no caller in workflow/, whose bare `method` key
# otherwise matched `sbs.method` and blocked a valid four_color config.
# shared/cellpose_training.py `mode` added 2026-10-06: a function argument that
# matched the mozzarellm `mode: cot` key and blocked a valid aggregate config.
_LIB_INTERNAL_GUARD_ALLOWLIST = {
    ("cluster/cluster_eval.py", "metric"),
    ("classify/train.py", "scaler_type"),
    ("classify/train.py", "model_type"),
    ("cluster/cluster_analysis.py", "normalize_method"),
    ("phenotype/segment_secondary_object.py", "method"),
    ("shared/cellpose_training.py", "mode"),
}


def _is_allowlisted_internal_guard(guard: dict, lib_dir: Path) -> bool:
    try:
        rel = Path(guard["file"]).resolve().relative_to(lib_dir.resolve()).as_posix()
    except ValueError:
        rel = guard["file"]
    return (rel, guard["key"]) in _LIB_INTERNAL_GUARD_ALLOWLIST


def analyze_brieflow(brieflow_dir: Path) -> BrieflowSurface:
    """Walk brieflow/workflow/ and derive the config + rule surface."""
    surface = BrieflowSurface()
    workflow = brieflow_dir / "workflow"

    rules_dir = workflow / "rules"
    if rules_dir.exists():
        for smk in rules_dir.glob("*.smk"):
            surface.n_smk_files += 1
            for r in find_rules_without_resources(smk):
                if not r["has_mem_mb"] and not r["rule"].startswith("all_"):
                    # all_<module> rules are aggregator stubs; ignore.
                    surface.rules_without_resources.append(r)

    scripts_dir = workflow / "scripts"
    if scripts_dir.exists():
        for py in scripts_dir.rglob("*.py"):
            surface.n_py_files += 1
            try:
                src = py.read_text()
            except OSError:
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            v = _ConfigKeyVisitor()
            v.visit(tree)
            surface.required_keys.update(v.keys_required)
            surface.optional_keys.update(v.keys_optional)
            surface.params_used.update(v.params_used)
            surface.accepted_value_guards.extend(parse_unknown_value_guards(src, str(py)))

    # workflow/lib: guard-only scan. Deliberately NOT running _ConfigKeyVisitor
    # here — lib functions take a `config` dict as a plain parameter in several
    # modules (classify/apply.py, preprocess/preprocess.py,
    # shared/rule_utils.py, ...), which is a different object from the
    # snakemake-injected `config`/`snakemake.config` that scripts read from.
    # The visitor can't tell them apart by name alone; running it over lib
    # pulls in local-parameter keys (e.g. "threshold", "expected_edges",
    # "mode") as spurious CONFIG_KEY_MISSING (HIGH) checks that would false-
    # positive on any real screen config. Value guards (parsed via regex on
    # the raise-ValueError message shape, not the `config` name) don't have
    # that ambiguity, so only they are collected from lib.
    lib_dir = workflow / "lib"
    if lib_dir.exists():
        for py in lib_dir.rglob("*.py"):
            surface.n_lib_py_files += 1
            try:
                src = py.read_text()
            except OSError:
                continue
            lib_guards = parse_unknown_value_guards(src, str(py))
            lib_guards += parse_supported_dict_guards(src, str(py))
            surface.accepted_value_guards.extend(
                g for g in lib_guards if not _is_allowlisted_internal_guard(g, lib_dir)
            )

    return surface


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def find_config_value(config: dict, key: str) -> tuple[bool, object]:
    """Search config recursively for a key. Returns (found, value).

    Checks top-level keys first, then any dict subsection (phenotype:, sbs:, etc.).
    The brieflow config has a nested structure where the same logical key may
    appear under multiple sections.
    """
    if key in config:
        return True, config[key]
    for v in config.values():
        if isinstance(v, dict):
            found, val = find_config_value(v, key)
            if found:
                return True, val
    return False, None


def validate_config(
    config_path: Path,
    surface: BrieflowSurface,
    skip_keys: set[str] | None = None,
) -> list[Issue]:
    """Validate the config against the brieflow surface.

    Returns list of Issues. Some keys are framework-internal and not user-facing
    (e.g. snakemake's own attributes) — skip_keys filters them out.
    """
    skip_keys = skip_keys or set()
    issues: list[Issue] = []

    if not config_path.exists():
        issues.append(
            Issue(
                severity="HIGH",
                code="CONFIG_MISSING",
                message=f"Config file {config_path} does not exist",
                suggestion="Create config/config.yml from brieflow/tests/small_test_analysis/config/config.yml",
            )
        )
        return issues

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        issues.append(
            Issue(
                severity="HIGH",
                code="CONFIG_YAML_ERROR",
                message=f"YAML parse error: {e}",
                file=str(config_path),
            )
        )
        return issues

    # 1. Accepted-value validation (catches the cp_method case).
    # For each guard, find the corresponding value in the config and check
    # against the accepted set.
    for guard in surface.accepted_value_guards:
        key = guard["key"]
        if key in skip_keys:
            continue
        found, value = find_config_value(config, key)
        if not found:
            continue  # missing-key check is below

        # Conditional guards (e.g. sbs.method's accepted set depends on
        # sbs.chemistry) narrow the accepted set to the dependency's current
        # value when it's resolvable; otherwise fall back to the flat union
        # so we don't miss a value that's invalid under every dependency
        # value. An unresolvable/invalid dependency is reported by its own
        # guard (e.g. the chemistry guard), not duplicated here.
        accepted = guard["accepted"]
        depends_on = guard.get("depends_on")
        if depends_on:
            dep_found, dep_value = find_config_value(config, depends_on)
            if dep_found and dep_value in guard.get("accepted_by_dependency", {}):
                accepted = guard["accepted_by_dependency"][dep_value]

        if isinstance(value, str) and value not in accepted:
            issues.append(
                Issue(
                    severity="HIGH",
                    code="CONFIG_VALUE_NOT_ACCEPTED",
                    message=(
                        f"`{key}: {value}` not in accepted set {accepted}"
                        + (
                            f" (for `{depends_on}: {dep_value}`)"
                            if depends_on and dep_found
                            else ""
                        )
                    ),
                    file=guard["file"],
                    line=guard["line"],
                    suggestion=(
                        f"Set `{key}` to one of: "
                        + ", ".join(accepted)
                        + f" (validation site: {Path(guard['file']).name}:{guard['line']})"
                    ),
                )
            )

    # 2. Required-key validation. brieflow's pattern is mostly
    # `snakemake.params.X` where X is set in the rule's params: block from
    # config. For preflight purposes both surfaces map to "must exist
    # somewhere in the config tree". Direct config[K] lookups are HIGH
    # severity (the script will hard-fail); params lookups are MEDIUM
    # because rule-level params: blocks may have defaults.
    for key in sorted(surface.required_keys):
        if key in skip_keys:
            continue
        found, _ = find_config_value(config, key)
        if not found:
            issues.append(
                Issue(
                    severity="HIGH",
                    code="CONFIG_KEY_MISSING",
                    message=f"`{key}` accessed via `config[{key!r}]` but not found in config",
                    suggestion=(
                        f"Add `{key}` to config (or a section of config). "
                        "Check brieflow/tests/small_test_analysis/config/config.yml for canonical schema."
                    ),
                )
            )

    # `snakemake.params.X` is brieflow's primary access pattern but not all
    # params come straight from config[X] — many are derived in the rule's
    # `params:` block. So this is LOW severity (informational) by default.
    # The cp_method-style accepted-value guards (HIGH) above are the
    # value-add: they fire on *known-bad* values, not just absence.
    missing_params = [
        k
        for k in sorted(surface.params_used)
        if k not in skip_keys and not find_config_value(config, k)[0]
    ]
    if missing_params:
        issues.append(
            Issue(
                severity="LOW",
                code="PARAMS_NOT_IN_CONFIG",
                message=(
                    f"{len(missing_params)} `snakemake.params.X` keys not "
                    "found in config (may be derived in rule params: blocks "
                    "or set from non-config sources — informational only)"
                ),
                suggestion=(
                    f"Sample: {missing_params[:8]}{'...' if len(missing_params) > 8 else ''}. "
                    "Use --json to see the full list. If a run actually "
                    "fails on a KeyError for one of these, it should be "
                    "added to config or the rule's params: block fixed."
                ),
            )
        )

    # 3. Rules-without-resources surveillance (LOW severity — informational
    # but important for OOM risk at well/full scale).
    if surface.rules_without_resources:
        rules_str = ", ".join(sorted({r["rule"] for r in surface.rules_without_resources}))
        issues.append(
            Issue(
                severity="LOW",
                code="RULES_WITHOUT_RESOURCES",
                message=(
                    f"{len(surface.rules_without_resources)} rules in "
                    "brieflow have no explicit `resources:` block — they "
                    "will inherit the global default mem_mb=3000 unless "
                    "overridden via mem_recommendations.json"
                ),
                suggestion=(
                    f"Rules at risk: {rules_str}. "
                    "Run /brieflow-tune-memory after each successful run "
                    "to keep mem_recommendations.json up to date. "
                    "Submit a PR to brieflow adding `resources:` blocks "
                    "to merge/aggregate rules."
                ),
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Mem-cap coverage check (HARDENING_TODO #18)
#
# A rule without a `resources:` block in its .smk inherits the global
# default `mem_mb=3000`. That's almost always wrong at well/full scale.
# This pass cross-references resources-less rules against:
#   1. mem_recommendations.json (canonical per-screen sizing)
#   2. slurm/config.yaml's set-resources mapping (per-screen overrides)
# A rule with no coverage in either gets a HIGH severity flag.
# ---------------------------------------------------------------------------


def load_mem_recs(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def load_slurm_set_resources(slurm_config_path: Path) -> dict[str, int]:
    """Parse `set-resources:` from slurm/config.yaml. Returns {rule: mem_mb}."""
    if not slurm_config_path.exists():
        return {}
    try:
        with open(slurm_config_path) as f:
            cfg = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError):
        return {}
    set_resources = cfg.get("set-resources") or {}
    out: dict[str, int] = {}
    for rule, res in set_resources.items():
        if isinstance(res, dict) and "mem_mb" in res:
            try:
                out[rule] = int(res["mem_mb"])
            except (TypeError, ValueError):
                continue
    return out


_PLUGIN_DATA = Path(__file__).resolve().parent.parent / "data"


def load_mem_recs_with_baseline(analysis_dir: Path) -> dict:
    """Per-screen mem_recommendations.json if present, else the plugin baseline.

    Mirrors brieflow_run's fallthrough so preflight and the actual run agree on
    coverage — without it, a fresh screen (no per-screen file) looked empty here
    while the run sized every rule from the bundled baseline.
    """
    for fp in (
        analysis_dir / "slurm" / "mem_recommendations.json",
        analysis_dir / "cluster_optimization" / "results" / "mem_recommendations.json",
        analysis_dir / "mem_recommendations.json",
    ):
        if fp.exists():
            return load_mem_recs(fp)
    return load_mem_recs(_PLUGIN_DATA / "mem_recommendations_baseline.json")


def load_scaling_formula_rules(analysis_dir: Path) -> set[str]:
    """Rule names brieflow_resolve_resources can size from a scaling formula.

    Reads the plugin's scaling_formulas_baseline.yaml plus any per-screen override.
    A rule present here resolves to a positive mem_mb at run time even with no
    mem_recommendations entry, so preflight must count it as covered (it's
    resolve_resources' layer 2). Geometry-free: presence of a formula, not its value.
    """
    rules: set[str] = set()
    for fp in (
        _PLUGIN_DATA / "scaling_formulas_baseline.yaml",
        analysis_dir / "slurm" / "scaling_formulas.yaml",
    ):
        if not fp.exists():
            continue
        try:
            with open(fp) as f:
                d = yaml.safe_load(f) or {}
        except (yaml.YAMLError, OSError):
            continue
        section = d.get("rules") if isinstance(d, dict) else None
        if isinstance(section, dict):
            rules.update(section)
    return rules


def validate_mem_cap_coverage(
    surface: BrieflowSurface,
    mem_recs: dict,
    slurm_set_resources: dict[str, int],
    backend: str = "slurm",
    scaling_rules: set[str] | None = None,
) -> list[Issue]:
    """Flag resources-less rules with no mem-cap coverage anywhere.

    A rule is "covered" if any of these is true — the same layers
    brieflow_resolve_resources applies at run time, so preflight and the run agree:
      - It has an explicit resources: mem_mb in its .smk (i.e. NOT in
        surface.rules_without_resources)
      - mem_recommendations.json (per-screen, else the plugin baseline) has an
        entry for the rule (including a group_member_stub that resolves to a
        group with a cap)
      - scaling_formulas has a formula for the rule (sized by geometry at run time)
      - slurm/config.yaml's set-resources maps mem_mb for the rule
    """
    issues: list[Issue] = []
    uncovered: list[dict] = []
    scaling_rules = scaling_rules or set()

    resourceless_rules = {r["rule"] for r in surface.rules_without_resources}

    def covered_in_mem_recs(rule: str) -> bool:
        if rule in mem_recs:
            entry = mem_recs[rule]
            if entry.get("mem_mb_recommended") is not None:
                return True
            # Group stub: covered iff the parent group has a cap.
            group = entry.get("group")
            if group and group in mem_recs and mem_recs[group].get("mem_mb_recommended"):
                return True
        return False

    for rule_info in surface.rules_without_resources:
        rule = rule_info["rule"]
        if covered_in_mem_recs(rule):
            continue
        if rule in scaling_rules:
            continue
        if rule in slurm_set_resources:
            continue
        uncovered.append(rule_info)

    if uncovered:
        rules_str = ", ".join(sorted({r["rule"] for r in uncovered}))
        # On the local backend, mem_mb is advisory scheduling metadata, not a hard
        # cap the scheduler enforces by killing jobs (that's a slurm concept). The
        # real OOM lever locally is concurrency (--cores), so uncovered mem caps are
        # informational here, not a submit-blocker.
        local = backend == "local"
        issues.append(
            Issue(
                severity="LOW" if local else "HIGH",
                code="MEM_CAP_UNCOVERED",
                message=(
                    f"{len(uncovered)} rule(s) have no `resources:` block / "
                    "mem_recommendations.json / scaling_formulas / slurm "
                    "`set-resources:` cap"
                    + (
                        " — on the local backend mem_mb is advisory (not a hard kill "
                        "cap), so this won't OOM by itself; bound `--cores` to keep "
                        "peak concurrency within machine RAM."
                        if local
                        else " — will fall back to the global default `mem_mb=3000` "
                        "and almost certainly OOM at well/full scale."
                    )
                ),
                suggestion=(
                    f"Uncovered rules: {rules_str}. These resolve through no layer "
                    "resolve_resources knows — most often a rule brieflow added that "
                    "the plugin baseline hasn't caught up to. Real fix: add each to "
                    "data/scaling_formulas_baseline.yaml (pick the scaling label for "
                    "its dimension). "
                    + (
                        "Local: mem_mb is advisory — bound `--cores` for this run."
                        if local
                        else "Stopgap for one run: per-rule `set-resources` in "
                        "slurm/config.yaml, or pre-seed mem_recommendations.json."
                    )
                ),
            )
        )
    elif resourceless_rules:
        issues.append(
            Issue(
                severity="INFO",
                code="MEM_CAP_COVERED",
                message=(
                    f"All {len(resourceless_rules)} resources-less rules "
                    "have coverage via mem_recommendations.json / scaling_formulas / "
                    "slurm/config.yaml set-resources."
                ),
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _color(s: str, severity: str) -> str:
    """Add a unicode marker for quick visual scanning. ASCII fallbacks if needed."""
    markers = {"HIGH": "✗", "MEDIUM": "⚠", "LOW": "ℹ", "INFO": "•"}
    return f"{markers.get(severity, '?')} {s}"


def print_report(issues: list[Issue], surface: BrieflowSurface) -> None:
    print("=" * 70)
    print("brieflow_preflight — config validation report")
    print("=" * 70)
    print(
        f"Surface: {surface.n_smk_files} .smk files, "
        f"{surface.n_py_files} .py scripts, "
        f"{surface.n_lib_py_files} .py lib modules, "
        f"{len(surface.required_keys)} required keys, "
        f"{len(surface.accepted_value_guards)} accepted-value guards, "
        f"{len(surface.rules_without_resources)} no-resources rules."
    )
    print()

    if not issues:
        print(_color("No issues found.", "INFO"))
        return

    by_severity: dict[str, list[Issue]] = {}
    for issue in issues:
        by_severity.setdefault(issue.severity, []).append(issue)

    for sev in ["HIGH", "MEDIUM", "LOW", "INFO"]:
        if sev not in by_severity:
            continue
        print(f"--- {sev} ({len(by_severity[sev])}) ---")
        for issue in by_severity[sev]:
            print(_color(f"[{issue.code}] {issue.message}", sev))
            if issue.file:
                print(f"      site: {issue.file}:{issue.line}")
            if issue.suggestion:
                print(f"      fix:  {issue.suggestion}")
        print()


# ---------------------------------------------------------------------------
# screen.yaml required-input gate
# ---------------------------------------------------------------------------


def validate_screen_yaml_inputs(analysis_dir: Path) -> list[Issue]:
    """Gate the operator-supplied screen.yaml inputs that the pipeline can NOT
    derive and that have bitten real onboardings:

      - library.raw_design_table_fp (the CRISPR barcode/design TSV) must be set
        and the file must exist in the repo. It is a first-class required input
        with no auto-probe fallback — a blank or missing file fails the SBS
        phase deep into a run.
      - data.input_sbs_dir / data.input_phenotype_dir must be filled with the
        VM-local raw-data paths. They are operator-locked (screen.yaml), NOT
        notebook-derived; left null they surface only as runner gaps.
      - cellular.well_annotations, when set, must name a table beside screen.yaml
        whose header has plate, well and the cellular.split_by column.
      - cellular.split_by needs cellular.well_annotations, the table that holds it.

    Returns HIGH issues so the preflight gate aborts the run early. No-op (empty)
    when screen.yaml is absent — legacy/config-only screens are unaffected.
    """
    screen_yaml = analysis_dir / "screen.yaml"
    if not screen_yaml.exists():
        return []

    issues: list[Issue] = []
    try:
        doc = yaml.safe_load(screen_yaml.read_text()) or {}
    except yaml.YAMLError as e:
        return [
            Issue(
                "HIGH",
                "screen-yaml-parse",
                f"screen.yaml does not parse: {e}",
                file=str(screen_yaml),
            )
        ]

    library = doc.get("library") or {}
    design_fp = library.get("raw_design_table_fp")
    if not design_fp:
        issues.append(
            Issue(
                "HIGH",
                "barcode-table-missing",
                "library.raw_design_table_fp is blank — the barcode/design TSV "
                "is a required SBS input and cannot be derived.",
                file=str(screen_yaml),
                suggestion="Set it to the repo path of the design TSV (e.g. "
                "config/pool10_design.tsv) and place the file there.",
            )
        )
    elif not (analysis_dir / design_fp).exists():
        issues.append(
            Issue(
                "HIGH",
                "barcode-table-missing",
                f"barcode/design table not found: {design_fp}",
                file=str(screen_yaml),
                suggestion=f"Place the design TSV at {analysis_dir / design_fp} "
                "(it co-travels with screen.yaml from the upload location).",
            )
        )

    data = doc.get("data") or {}
    for key in ("input_sbs_dir", "input_phenotype_dir"):
        if not data.get(key):
            issues.append(
                Issue(
                    "HIGH",
                    "input-dir-missing",
                    f"data.{key} is null — raw-data dirs are operator-locked in "
                    "screen.yaml, not notebook-derived.",
                    file=str(screen_yaml),
                    suggestion="Fill it with the VM-local raw-data path "
                    "(e.g. /data/<screen>/...) at S7 setup.",
                )
            )

    cellular = doc.get("cellular") or {}
    ann, split_by = cellular.get("well_annotations"), cellular.get("split_by")
    group_by, reference = cellular.get("group_by"), cellular.get("reference_group")
    group_cols = list(group_by) if isinstance(group_by, (list, tuple)) else [group_by] if group_by else []
    ann_fp = analysis_dir / ann if ann else None
    if ann_fp is not None and not ann_fp.exists():
        issues.append(
            Issue(
                "HIGH",
                "well-annotations-missing",
                f"cellular.well_annotations names {ann}, which is not beside screen.yaml",
                file=str(screen_yaml),
                suggestion="Copy the per-well table (plate, well, one column per variable) "
                "next to screen.yaml, or set cellular.well_annotations to null.",
            )
        )
    elif ann_fp is not None:
        sep = "," if ann_fp.suffix.lower() == ".csv" else "\t"
        with open(ann_fp, newline="") as fh:
            rows = [[c.strip() for c in r] for r in csv.reader(fh, delimiter=sep)]
        header = rows[0] if rows else []
        need = ["plate", "well"] + ([split_by] if split_by else []) + group_cols
        absent = [c for c in need if c not in header]
        if absent:
            issues.append(
                Issue(
                    "HIGH",
                    "well-annotations-columns",
                    f"{ann} lacks column(s) {absent}; its header is {header}",
                    file=str(ann_fp),
                    suggestion="The table needs plate, well and the cellular.split_by and "
                    "cellular.group_by columns.",
                )
            )
        elif reference and len(group_cols) == 1:
            values = {r[header.index(group_cols[0])] for r in rows[1:] if len(r) == len(header)}
            if str(reference) not in values:
                issues.append(
                    Issue(
                        "HIGH",
                        "reference-group-not-in-table",
                        f"cellular.reference_group {reference!r} is not a value of "
                        f"{group_cols[0]!r} in {ann} ({sorted(values)})",
                        file=str(screen_yaml),
                        suggestion="Set cellular.reference_group to the vehicle or untreated "
                        "value exactly as the table spells it.",
                    )
                )
    for key, value in (("group_by", group_by), ("reference_group", reference)):
        if value and not ann:
            issues.append(
                Issue(
                    "HIGH",
                    f"{key.replace('_', '-')}-without-annotations",
                    f"cellular.{key} is {value!r} but cellular.well_annotations is not set",
                    file=str(screen_yaml),
                    suggestion="Name the per-well table in cellular.well_annotations.",
                )
            )
    if reference and not group_by:
        issues.append(
            Issue(
                "HIGH",
                "reference-group-without-group-by",
                f"cellular.reference_group is {reference!r} but cellular.group_by is not set",
                file=str(screen_yaml),
                suggestion="Set cellular.group_by to the table column that holds the reference value.",
            )
        )
    if split_by and not ann:
        issues.append(
            Issue(
                "HIGH",
                "split-by-without-annotations",
                f"cellular.split_by is {split_by!r} but cellular.well_annotations is not set",
                file=str(screen_yaml),
                suggestion="Name the per-well table that holds the split column in "
                "cellular.well_annotations.",
            )
        )

    return issues


_NUCLEAR_ALIASES = (
    "hoechst",
    "hoechst 33342",
    "hoechst33342",
    "dna",
    "nuclei",
    "nucleus",
    "nuclear",
)


def _aggregate_splits_combos_on_underscore(brieflow_dir: Path | None) -> bool:
    """True unless the brieflow checkout parses channel combos against channel names (#314 fix)."""
    if brieflow_dir is None:
        return True
    split = brieflow_dir / "workflow" / "scripts" / "aggregate" / "split_datasets.py"
    try:
        return 'channel_combo.split("_")' in split.read_text()
    except OSError:
        return True


def validate_sort_calls(analysis_dir: Path, config_path: Path) -> list[Issue]:
    """A DNA-barcode screen must sort calls by peak: count is the mRNA ordering, and on DNA
    spots it changes which reads are called without raising an error."""
    try:
        screen = yaml.safe_load((analysis_dir / "screen.yaml").read_text()) or {}
        config = yaml.safe_load(Path(config_path).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    target = str((screen.get("library") or {}).get("detection_target") or "").strip().upper()
    sort_calls = (config.get("sbs") or {}).get("sort_calls")
    if target != "DNA" or sort_calls in (None, "peak"):
        return []
    return [
        Issue(
            "HIGH",
            "sort-calls-not-peak-for-dna",
            f"library.detection_target is DNA but sbs.sort_calls is {sort_calls!r}; DNA barcodes "
            "sort calls by peak.",
            file=str(config_path),
            suggestion="set SORT_CALLS to \"peak\" in the SBS notebook (the interview derives it from "
            "detection_target) and re-run the SBS phase",
        )
    ]


def validate_channel_conventions(
    analysis_dir: Path, brieflow_dir: Path | None = None
) -> list[Issue]:
    """Channel-name conventions the marimo notebooks hardcode, checked on
    screen.yaml so they fail in seconds, not after preprocess:

    * phenotype: a channel named exactly ``DAPI`` must exist —
      the phenotype notebook does ``CHANNEL_NAMES.index("DAPI")`` (a screen that named its
      nuclear stain "Hoechst" and paused after a full SBS run);
    * sbs: ``DAPI`` must be channel 0 when ``DAPI_CYCLE != CYTO_CYCLE`` — the
      segmentation composite is reordered DAPI-first but ``dapi_index`` /
      ``cyto_index`` are not remapped, so cellpose segments a spot channel
      (on one screen's test tile: 84% of nuclei dropped);
    * phenotype: no ``_`` in channel names while ``brieflow_dir`` still splits combos on it;
    * sbs: four spot channels must be named G/T/A/C (BASES is derived from the names)."""
    screen_yaml = analysis_dir / "screen.yaml"
    if not screen_yaml.exists():
        return []
    try:
        doc = yaml.safe_load(screen_yaml.read_text()) or {}
    except yaml.YAMLError:
        return []
    issues: list[Issue] = []
    ph = [c for c in ((doc.get("phenotype") or {}).get("channels") or []) if isinstance(c, dict)]
    ph_names = [str(c.get("biological_name") or c.get("name") or "") for c in ph]
    if ph_names and "DAPI" not in ph_names:
        alias = next((n for n in ph_names if n.lower() in _NUCLEAR_ALIASES), None)
        issues.append(
            Issue(
                "HIGH",
                "phenotype-nuclear-channel-not-dapi",
                f"phenotype.channels has no channel named DAPI ({ph_names}); the phenotype notebook hardcodes "
                f'CHANNEL_NAMES.index("DAPI") and fails at the notebook run.',
                file=str(screen_yaml),
                suggestion=(
                    f'rename the nuclear channel "{alias}" to biological_name "DAPI" (keep the stain in the '
                    "description) and re-resolve the interview"
                    if alias
                    else 'name the nuclear-stain channel biological_name "DAPI" and re-resolve the interview'
                ),
            )
        )
    underscored = [n for n in ph_names if "_" in n]
    if underscored and _aggregate_splits_combos_on_underscore(brieflow_dir):
        issues.append(
            Issue(
                "HIGH",
                "phenotype-channel-name-underscore",
                f"phenotype channel name(s) {underscored} contain '_': aggregate joins channel "
                "combos with '_' and splits them again, so these channels' features are dropped "
                "(cheeseman-lab/brieflow#314).",
                file=str(screen_yaml),
                suggestion="rename without '_' (e.g. BODIPY_Low -> BODIPYLow) and re-resolve the "
                "interview, until brieflow#314 is fixed",
            )
        )
    iss = doc.get("iss") or {}
    sbs = [c for c in (iss.get("channels") or []) if isinstance(c, dict)]
    sbs_names = [str(c.get("name") or "") for c in sbs]
    spots = [n for n in sbs_names if n != "DAPI"]
    if len(spots) == 4 and sorted(spots) != sorted("GTAC"):
        issues.append(
            Issue(
                "HIGH",
                "sbs-channels-not-base-named",
                f"iss.channels names the four base channels {spots}; the SBS notebook derives BASES from "
                "channels named G/T/A/C, so dye names give BASES=[] and no base calls.",
                file=str(screen_yaml),
                suggestion="name each base channel by its base (G/T/A/C, per the cycle order), "
                "keep the dye in a comment, then re-resolve the interview",
            )
        )
    bcn = iss.get("background_channel_nucleus")
    dapi_cycle = (bcn.get("cycle") if isinstance(bcn, dict) else None) or 1
    bcc = iss.get("background_channel_cell")
    cyto_cycle = bcc.get("cycle") if isinstance(bcc, dict) else None
    if (
        sbs_names
        and "DAPI" in sbs_names
        and sbs_names[0] != "DAPI"
        and cyto_cycle is not None
        and dapi_cycle != cyto_cycle
    ):
        issues.append(
            Issue(
                "HIGH",
                "sbs-dapi-not-first",
                f"iss.channels puts DAPI at index {sbs_names.index('DAPI')} ({sbs_names}) while DAPI_CYCLE "
                f"({dapi_cycle}) != CYTO_CYCLE ({cyto_cycle}): the segmentation composite is reordered DAPI-first "
                "but dapi_index/cyto_index are not remapped — cellpose would segment a spot channel.",
                file=str(screen_yaml),
                suggestion="Put DAPI first in iss.channels (convert with that order, or reorder the store with "
                "brieflow_reorder_channels.py), or use the same cycle for DAPI and the background channel.",
            )
        )
    return issues


def validate_dye_balance_probe(analysis_dir: Path) -> list[Issue]:
    """Gate on `.brieflow/dye_balance_probe.json` (written by
    brieflow_dye_balance_probe.py, run by the runner before the SBS phase):
    MEDIUM when the probe found a calibration that beats the stock call but the config
    does not carry it, so a run that could decode twice as well does not start silently
    on the stock caller. No-op when no report exists, or when the probe found nothing
    worth applying. Never HIGH: a stock call is a valid call, just a weaker one."""
    report_path = analysis_dir / ".brieflow" / "dye_balance_probe.json"
    if not report_path.exists():
        return []
    try:
        report = json.loads(report_path.read_text())
    except (OSError, ValueError) as e:
        return [
            Issue(
                "MEDIUM",
                "dye-balance-probe-unreadable",
                f"dye_balance_probe.json unreadable: {e}",
                file=str(report_path),
            )
        ]
    if report.get("verdict") != "improved":
        return []
    config_path = analysis_dir / "config" / "config.yml"
    try:
        config = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []
    combi = (config.get("sbs") or {}).get("combinatorial") or {}
    missing = [
        key
        for key, want in (
            ("dye_scale", report.get("dye_scale")),
            ("brightness_regions", report.get("brightness_regions")),
        )
        if want and combi.get(key) != want
    ]
    if not missing:
        return []
    gain = report.get("gain")
    return [
        Issue(
            "MEDIUM",
            "dye-balance-not-applied",
            f"the dye-balance probe raised exact-library reads by {gain:+.0%} with "
            f"dye_scale={report.get('dye_scale')} brightness_regions={report.get('brightness_regions')}, "
            f"but sbs.combinatorial is missing {missing} — this run will use the stock call",
            file=str(report_path),
        )
    ]


def validate_base_order_probe(analysis_dir: Path) -> list[Issue]:
    """Gate on `.brieflow/base_order_probe.json` (written by
    brieflow_base_order_probe.py, run by the runner before the SBS phase):
    HIGH when the data support a different channel↔base assignment than the one
    configured, MEDIUM when the probe could not tell (best match ≈ chance, or too
    few reads). No-op when no report exists."""
    report_path = analysis_dir / ".brieflow" / "base_order_probe.json"
    if not report_path.exists():
        return []
    try:
        report = json.loads(report_path.read_text())
    except (OSError, ValueError) as e:
        return [
            Issue(
                "MEDIUM",
                "base-order-probe-unreadable",
                f"base_order_probe.json unreadable: {e}",
                file=str(report_path),
            )
        ]
    verdict = report.get("verdict")
    if verdict == "ok":
        return []
    best = report.get("best") or {}
    configured = report.get("configured") or {}
    if verdict == "library-region-mismatch":
        d = report.get("diagnostics") or {}
        return [
            Issue(
                "HIGH",
                "base-order-library-region-mismatch",
                f"barcode library prefixes are {d.get('prefix_length')} wide but the configured MAP region "
                f"(map_start/map_end) is {d.get('region_length')} wide — no read can ever match. "
                f"Dropped-cycle scan: {d.get('dropped_cycle_scan')}.",
                file=str(report_path),
                suggestion="Re-standardize the library and the map region with the SAME skip-cycle settings "
                "(SKIP_CYCLES_MAP ↔ MAP_START/MAP_END) and rerun.",
            )
        ]
    if verdict == "sequencing-order-mismatch":
        d = report.get("diagnostics") or {}
        return [
            Issue(
                "HIGH",
                "base-order-sequencing-order-mismatch",
                f"the data were sequenced {d.get('inferred_sequencing_order')} but screen.yaml says "
                f"{d.get('configured_sequencing_order')}: the RECOMB library matches where the MAP region is "
                f"configured (and vice versa) on tile {report.get('well')}/{report.get('tile')}. Staging and "
                f"preprocess are fine.",
                file=str(report_path),
                suggestion=f"Set library.sequencing_order: {report.get('suggested_sequencing_order')} in screen.yaml, "
                "re-resolve the interview (MAP/RECOMB positions are derived), rerun sbs — no re-staging.",
            )
        ]
    if verdict == "cycle-window-mismatch":
        scan = ((report.get("diagnostics") or {}).get("configured_calls") or {}).get(
            "map_window_scan"
        )
        return [
            Issue(
                "HIGH",
                "base-order-cycle-window-mismatch",
                f"the MAP barcode lives in staged {report.get('suggested_map_window')}, not the configured "
                f"cycles {report.get('prefix_region', ['?', '?'])[0] + 1 if report.get('prefix_region') else '?'}-"
                f"{report.get('prefix_region', ['?', '?'])[1] if report.get('prefix_region') else '?'} — the staged "
                f"cycle set is wrong (wrong measurements, or an aborted imaging session mixed in). Window scan: {scan}.",
                file=str(report_path),
                suggestion="Re-map measurement→cycle in the staging (drop the non-SBS measurements) and re-run "
                "preprocess for SBS, or set the MAP/RECOMB regions to the staged cycles that carry them.",
            )
        ]
    if verdict == "mismatch":
        return [
            Issue(
                "HIGH",
                "base-order-mismatch",
                f"channel↔base assignment contradicted by the data on tile "
                f"{report.get('well')}/{report.get('tile')}: configured bases {configured.get('bases')} "
                f"match {configured.get('match')}, best {best.get('bases')}"
                f"{' (cycles reversed)' if best.get('reversed') else ''} match {best.get('match')} "
                f"(chance {report.get('chance_match')}).",
                file=str(report_path),
                suggestion=f"Set sbs bases / channel names to {report.get('suggested_bases')} in screen.yaml "
                "(and the combinatorial code / cycle order accordingly), re-resolve the interview, rerun.",
            )
        ]
    return [
        Issue(
            "MEDIUM",
            f"base-order-{verdict}",
            f"base-order probe could not confirm the channel↔base assignment on tile "
            f"{report.get('well')}/{report.get('tile')}: best match {best.get('match')} vs chance "
            f"{report.get('chance_match')} (n_reads={report.get('n_reads')}).",
            file=str(report_path),
            suggestion="Base order is not the problem or not testable yet — check alignment, "
            "THRESHOLD_READS and the cycle set before trusting SBS mapping.",
        )
    ]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--analysis-dir",
        required=True,
        type=Path,
        help="Screen analysis dir (contains config/, brieflow/).",
    )
    p.add_argument(
        "--brieflow-dir",
        type=Path,
        default=None,
        help="Override brieflow submodule path. "
        "Default: <analysis-dir>/../brieflow or <analysis-dir>/brieflow.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Override config path. Default: <analysis-dir>/config/config.yml",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output instead of human-readable.",
    )
    p.add_argument(
        "--severity-threshold",
        default="HIGH",
        choices=["HIGH", "MEDIUM", "LOW"],
        help="Exit code 1 if any issue at or above this severity is found.",
    )
    p.add_argument(
        "--backend",
        default="slurm",
        choices=["local", "slurm"],
        help="Compute backend. On 'local', mem_mb is advisory (not a hard kill "
        "cap), so uncovered mem caps are informational (LOW) rather than a HIGH "
        "submit-blocker — concurrency (--cores) is the OOM lever there.",
    )
    args = p.parse_args()

    analysis_dir = args.analysis_dir.resolve()
    if not analysis_dir.exists():
        print(f"ERROR: --analysis-dir {analysis_dir} does not exist.", file=sys.stderr)
        return 2

    if args.brieflow_dir:
        brieflow_dir = args.brieflow_dir.resolve()
    else:
        # Try common locations
        candidates = [
            analysis_dir.parent / "brieflow",
            analysis_dir / "brieflow",
            analysis_dir.parent.parent / "brieflow",
        ]
        brieflow_dir = next((c for c in candidates if c.exists()), candidates[0])

    if not brieflow_dir.exists():
        print(
            f"ERROR: brieflow submodule not found. Tried: {[str(c) for c in candidates]}",
            file=sys.stderr,
        )
        return 2

    config_path = args.config or analysis_dir / "config" / "config.yml"

    surface = analyze_brieflow(brieflow_dir)
    skip = {
        "input",
        "output",
        "params",
        "wildcards",
        "log",
        "threads",
        "resources",
        "shadow",
        "benchmark",
        "conda",
        "container",
        "envmodules",
        "message",
    }
    issues = validate_config(config_path, surface, skip_keys=skip)

    # screen.yaml required-input gate: barcode/design TSV + operator-locked
    # input dirs (caught early here rather than mid-run / at runner launch).
    issues.extend(validate_screen_yaml_inputs(analysis_dir))
    issues.extend(validate_channel_conventions(analysis_dir, brieflow_dir))
    issues.extend(validate_sort_calls(analysis_dir, config_path))
    issues.extend(validate_base_order_probe(analysis_dir))
    issues.extend(validate_dye_balance_probe(analysis_dir))

    # HARDENING #18: cross-reference resources-less rules against the same layers
    # brieflow_resolve_resources sizes from at run time — mem_recommendations.json
    # (per-screen, else plugin baseline) + scaling_formulas + slurm set-resources —
    # so preflight's coverage notion matches the run's on either backend.
    slurm_config = analysis_dir / "slurm" / "config.yaml"
    mem_recs = load_mem_recs_with_baseline(analysis_dir)
    slurm_set = load_slurm_set_resources(slurm_config)
    scaling_rules = load_scaling_formula_rules(analysis_dir)
    issues.extend(
        validate_mem_cap_coverage(
            surface,
            mem_recs,
            slurm_set,
            backend=args.backend,
            scaling_rules=scaling_rules,
        )
    )

    if args.json:
        out = {
            "analysis_dir": str(analysis_dir),
            "brieflow_dir": str(brieflow_dir),
            "config_path": str(config_path),
            "surface": {
                "n_smk_files": surface.n_smk_files,
                "n_py_files": surface.n_py_files,
                "n_lib_py_files": surface.n_lib_py_files,
                "required_keys": sorted(surface.required_keys),
                "accepted_value_guards": surface.accepted_value_guards,
                "rules_without_resources": surface.rules_without_resources,
            },
            "issues": [i.to_dict() for i in issues],
        }
        json.dump(out, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        print_report(issues, surface)

    threshold = SEVERITY_ORDER[args.severity_threshold]
    has_blocking = any(SEVERITY_ORDER[i.severity] <= threshold for i in issues)
    return 1 if has_blocking else 0


if __name__ == "__main__":
    sys.exit(main())
