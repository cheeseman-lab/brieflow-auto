#!/usr/bin/env python3
"""brieflow_buckets.py — the canonical resolution taxonomy.

Every interview row carries two orthogonal labels:

  * ``bucket``       — HOW the param gets resolved (the resolution strategy).
  * ``value_source`` — WHERE its current value actually came from (provenance).

This module is the single source of truth for both vocabularies, plus a
structural validator (:func:`validate_interview_buckets`) that the test suite
runs against the real ``data/interview_template.json`` so the taxonomy can't
drift silently.

Bucket families
---------------
operator        — asked up front. These are the ONLY families
                  ``resolve_all_phases`` collects as operator gaps:
                  ``user``, ``user_tuned_seed``, ``override``.
system-derived  — filled from the notebook or the data without asking:
                  ``auto``, ``auto-probe``, ``tuned-output``.
agent-resolved  — resolved during the run by an agent step (a sweep winner or a
                  run-time eval-table selection): ``agent-resolved``.
archival        — documentation-only; no marker block, never patched, never
                  asked: ``archival``.

``brieflow_wizard.py`` re-exports every public symbol here, so the historical
``from brieflow_wizard import VALID_VALUE_SOURCES`` (and the underscore-prefixed
internals) keeps working unchanged after the module split.
"""

from __future__ import annotations

from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Bucket families — the resolution strategy for each interview row.
# ---------------------------------------------------------------------------
OPERATOR_BUCKETS: frozenset[str] = frozenset({"user", "user_tuned_seed", "override"})
SYSTEM_DERIVED_BUCKETS: frozenset[str] = frozenset({"auto", "auto-probe", "tuned-output"})
AGENT_RESOLVED_BUCKETS: frozenset[str] = frozenset({"agent-resolved"})
ARCHIVAL_BUCKETS: frozenset[str] = frozenset({"archival"})

#: The closed set of buckets an interview row may carry.
ALL_BUCKETS: frozenset[str] = (
    OPERATOR_BUCKETS | SYSTEM_DERIVED_BUCKETS | AGENT_RESOLVED_BUCKETS | ARCHIVAL_BUCKETS
)

#: Operator-bucket set used by ``resolve_all_phases`` to decide which rows are
#: up-front questions. The rest are resolved without asking. Kept as a mutable
#: ``set`` (not ``frozenset``) for byte-identical behavior with callers.
_OPERATOR_BUCKETS: set[str] = set(OPERATOR_BUCKETS)

#: Buckets that don't expect a marker-block assignment. "Missing" marker
#: entries for these are downgraded to WARN by preflight; everything else
#: BLOCKs. Superset of the template buckets — also covers CSV-only labels
#: (``output``/``nonexistent``) that never appear in the interview template.
_NO_MARKER_BUCKETS: set[str] = {"archival", "output", "tuned-output", "nonexistent"}

# ---------------------------------------------------------------------------
# value_source enum — provenance of a row's current value.
# ---------------------------------------------------------------------------
# Semantics:
#   - "operator"        — typed by the operator in this session via AskUserQuestion
#   - "default"         — pure template default; no observation or screen.yaml lookup
#   - "auto-probe"      — wizard derived from data inspection (ND2 metadata,
#                         filename regex, dir walk, etc.)
#   - "gen"             — auto-generated via a helper (e.g., path expansion from
#                         a base + screen_id)
#   - "screen-yaml"     — pulled from screen.yaml via the row's `derivation` clause
#   - "tuned-confirmed" — operator accepted a notebook-tuned value (mapping rate,
#                         QC threshold, etc.)
#   - "lab-default"     — filled from lab_defaults.yaml notebook_defaults (a value
#                         the lab has standardized; operator/screen.yaml override it)
VALID_VALUE_SOURCES: set[str] = {
    "operator",
    "default",
    "auto-probe",
    "gen",
    "tuned-confirmed",
    "screen-yaml",
    "lab-default",
}

# Derivation types that read from screen.yaml and therefore require a `path`.
_SCREEN_YAML_DERIVATION_TYPES: frozenset[str] = frozenset(
    {"screen_yaml", "screen_yaml_list_field"}
)


def validate_interview_buckets(
    rows: Iterable[dict], *, compute_keys: Iterable[str] | None = None
) -> dict[str, Any]:
    """Structurally validate the interview template's resolution taxonomy.

    Checks every row for:

      * ``bucket`` present and in :data:`ALL_BUCKETS`.
      * screen.yaml-derived rows (``screen_yaml`` / ``screen_yaml_list_field``)
        carry a non-empty ``path``; list-field rows also carry a ``field``.
      * ``screen_yaml_computed`` rows name a ``compute`` key; if ``compute_keys``
        is supplied, that key must be registered (i.e. in
        ``_COMPUTED_DERIVATIONS``).
      * ``library_default`` rows carry a ``value`` key.

    Returns a report dict::

        {"ok": bool, "invalid_bucket": [...], "missing_path": [...],
         "missing_field": [...], "missing_compute": [...],
         "unknown_compute": [...], "missing_value": [...]}

    ``ok`` is True iff every list is empty. The resolver/probe coverage check
    (does every ``agent-resolved`` / ``auto-probe`` row have a live helper?)
    lives in ``brieflow_resolvers.validate_resolver_coverage`` to keep this
    module dependency-free.
    """
    compute = set(compute_keys) if compute_keys is not None else None
    report: dict[str, list[str]] = {
        "invalid_bucket": [],
        "missing_path": [],
        "missing_field": [],
        "missing_compute": [],
        "unknown_compute": [],
        "missing_value": [],
    }
    for row in rows:
        name = row.get("param_name", "<unnamed>")
        if row.get("bucket") not in ALL_BUCKETS:
            report["invalid_bucket"].append(f"{name}={row.get('bucket')!r}")
        deriv = row.get("derivation")
        if not isinstance(deriv, dict):
            continue
        dtype = deriv.get("type")
        if dtype in _SCREEN_YAML_DERIVATION_TYPES:
            if not deriv.get("path"):
                report["missing_path"].append(name)
            if dtype == "screen_yaml_list_field" and not deriv.get("field"):
                report["missing_field"].append(name)
        elif dtype == "screen_yaml_computed":
            key = deriv.get("compute")
            if not key:
                report["missing_compute"].append(name)
            elif compute is not None and key not in compute:
                report["unknown_compute"].append(f"{name}={key!r}")
        elif dtype == "library_default":
            if "value" not in deriv:
                report["missing_value"].append(name)
    out: dict[str, Any] = {"ok": all(not v for v in report.values())}
    out.update(report)
    return out
