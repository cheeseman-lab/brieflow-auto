---
description: Statically validate config/config.yml against the brieflow rule code. Catches "Unknown X" mismatches and missing required keys before submit.
---

# /brieflow-preflight

Activates the `brieflow-preflight` skill. Walks brieflow's `workflow/scripts/**/*.py` for `raise ValueError("Unknown ...")` guards + direct `config[K]` access patterns; validates the screen's config against them. HIGH-severity issues abort `/brieflow-module`.

See `skills/brieflow-preflight/SKILL.md` for what's checked.
