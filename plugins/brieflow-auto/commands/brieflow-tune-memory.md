---
description: Refresh slurm/mem_recommendations.json from accumulated efficiency CSVs. Preserves manual-override entries from post-OOM bumps.
---

# /brieflow-tune-memory

Activates the `brieflow-tune-memory` skill. Rolls up worst-case peak RSS per rule across all `logs/efficiency_*/*.csv` and writes recommended `mem_mb`. Idempotent; preserves `obs_source: manual_override` entries.

See `skills/brieflow-tune-memory/SKILL.md` for the schema + flags.
