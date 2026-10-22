---
name: brieflow-issue
description: Automatically open a GitHub issue for an identified Brieflow pipeline defect, including evidence and a backward-compatible fix proposal. Use after a run escalates a pipeline bug or when asked to report one; not for ordinary screen configuration, resource shortages, or unexplained low QC alone.
---

# Brieflow — Open an upstream issue

For an identified pipeline defect, open an actual issue on `cheeseman-lab/brieflow`
without a per-issue operator confirmation. A local report is an intermediate artifact,
not completion. Do not change pipeline source, commit, push, or implement the proposed fix
as part of this workflow. Maintainers decide how to fix the shared pipeline.

## Establish scope and evidence

Read the runner's `.brieflow/issue_draft.json` and `.md` when present, plus the matching
diagnostic bundle and failure-time `run_manifest.json`. These legacy filenames remain
local evidence inputs. For a direct module run, inspect `.brieflow/last_escalation.json`.
The recovery action's `should_file_issue` and `suggested_repo` are leads, not proof of a bug.

Check the failing rule and its documented input contract. Distinguish a Brieflow defect
or missing general capability from an invalid screen configuration, missing operator
information, resource exhaustion, or an uninvestigated QC failure. Do not file routine
operational problems as upstream bugs. Plugin-only defects belong in the plugin tracker,
not Brieflow's; this helper deliberately targets only Brieflow.

Use observed versus expected behavior and the smallest reproducible example available.
Do not invent a reproduction, a root cause, or a successful test. If the cause is uncertain,
label it as a hypothesis and say what evidence would distinguish the alternatives.
Read git status without committing. Keep the failing run's SHAs from the manifest, even
if the current checkout has moved; describe relevant uncommitted changes and acknowledge
when a commit alone cannot reproduce the failure.

Consult Brieflow's reporting guidance and current bug template:
https://github.com/cheeseman-lab/brieflow/blob/main/.github/ISSUE_TEMPLATE/bug_report.md
If network access is unavailable, use the matching checked-out template and record that
limitation. The helper implements its sections: description, reproduction, expected
behavior, screenshots if applicable, environment, and additional context. Use a `[BUG]`
title and the `bug` label. For a feature request rather than a defect, follow the current
upstream feature guidance instead of mislabeling it through the bug-report helper.

## Propose a backward-compatible fix

Inspect the relevant implementation and callers before suggesting a change. The proposal
must solve a general input or analysis requirement, not reshape Brieflow around this screen.
Include all three sections enforced by the helper:

- **Proposed fix (not applied):** the smallest general change, the likely affected component,
  and why existing configuration cannot already handle the case. No hardcoded screen names,
  paths, well IDs, barcode lengths, or special cases selected by dataset identity.
- **Backward compatibility:** name the existing behavior that must remain unchanged,
  including defaults, supported formats, public interfaces, output schemas and scientific
  meaning where relevant. New chemistry or acquisition behavior should use an explicit
  option or validated format detection that leaves old inputs on their existing path.
  Do not weaken validation, relax QC gates, silently discard inputs, change global defaults,
  or reinterpret old parameters just to make this screen pass. If the fix cannot be made
  backward compatible, state that limitation and request maintainer design guidance rather
  than presenting a breaking change as the recommendation.
- **Regression tests and verification:** propose tests for the failing input, representative
  previously supported inputs, unchanged defaults and outputs, and invalid-input handling.
  Distinguish tests actually run from tests proposed. Compatibility is a requirement until
  demonstrated, not a claim earned by writing a plausible patch.

## Open and record the issue

Before publishing, remove private dataset identifiers, absolute paths, credentials, URLs
with access tokens, and unpublished images/results unless explicitly cleared for sharing.
Use a synthetic reproducer where possible. Never upload the raw diagnostic bundle or full
manifest. If a useful report cannot be shared safely, stop and request the missing disclosure
permission; automatic issue filing does not authorize releasing private data.

Search existing open and closed Brieflow issues for the same rule, error, and input format.
Reuse and report an existing relevant issue URL instead of posting a duplicate. The helper
also deduplicates identical reports by a content fingerprint, but semantic duplicates still
require this check. Do not reopen or comment on someone else's issue automatically.

Resolve `SCRIPTS_DIR` from this skill's directory (`../../scripts`). Prepare the report
with the CLI or import `prepare_report` from `scripts/brieflow_issue.py`:

```bash
python "$SCRIPTS_DIR/brieflow_issue.py" prepare \
  --analysis-dir <absolute-analysis-dir> \
  --title '<concise failure>' \
  --description '<observed behavior and shareable evidence>' \
  --reproduction '<minimal reproduction steps>' \
  --expected-behavior '<documented expected behavior>' \
  --environment '<OS, relevant versions, browser or not applicable, compute backend>' \
  --context '<input contract, dirty-tree caveat, and uncertainty>' \
  --proposed-fix '<minimal general fix, not applied>' \
  --compatibility '<existing behavior preserved and how>' \
  --verification '<old-input and new-input tests; run versus proposed>'
```

Inspect the resulting JSON yourself for accuracy and public disclosure, then immediately
open it without asking the operator to approve the text:

```bash
python "$SCRIPTS_DIR/brieflow_issue.py" open --report <absolute-report-json>
```

This calls `gh issue create`, saves `filed_url` in the report under `.brieflow/issues/`,
and journals `issue-filed`. It requires authenticated GitHub CLI access. Return the issue
URL and summarize the compatibility constraint to the operator. Never call a report filed
unless an issue URL was obtained. Keep the analysis paused until the defect is resolved.

If GitHub is unavailable, retain the report and report the filing failure explicitly.
After an uncertain creation result, the helper searches for its fingerprint on the next
invocation but refuses a second creation if the outcome remains unknown. Inspect GitHub
and reconcile the receipt before attempting another creation; a timeout may have happened
after the issue was created. Do not prepare a differently worded report to bypass this guard.

## Exit

Return to `brieflow-auto` with the existing or newly opened issue URL, or a specific reason
filing could not complete. Pipeline edits and release decisions remain with maintainers.
