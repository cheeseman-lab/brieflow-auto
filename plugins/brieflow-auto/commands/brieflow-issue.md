---
description: File a GitHub issue on cheeseman-lab/brieflow. Auto-includes screen-repo + brieflow-submodule SHAs.
---

# /brieflow-issue

Activates the `brieflow-issue` skill. Investigates the runner's failure evidence, follows
Brieflow's bug-report template, and automatically opens an upstream issue with a minimal,
backward-compatible fix proposal and regression-test plan. Uses the failure-time commit
SHAs, checks for duplicates and private content, and returns a recorded GitHub issue URL.
No per-issue confirmation is required; pipeline source is not changed during the run.

See `skills/brieflow-issue/SKILL.md` for the body template + rules.
