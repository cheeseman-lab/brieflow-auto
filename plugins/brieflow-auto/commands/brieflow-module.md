---
description: Launch a brieflow pipeline run. Single-shot (tmux interactive) or auto-recover (synchronous, classifies failures, bumps mem on OOM, resumes — capped at N attempts).
---

# /brieflow-module

Activates the `brieflow-module` skill. Always runs preflight first; halts on HIGH-severity issues. Single-shot for interactive work, auto-recover for "complete the pipeline" / overnight runs. Uses `brieflow_runloop.py --phases` for full-pipeline runs.

See `skills/brieflow-module/SKILL.md` for modes, the auto-recover FSM, and rules.
