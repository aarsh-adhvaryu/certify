"""Per-host wrappers. The CLI is the guarantee; these are the ergonomics.

Enforcement is honest per host and the difference gets documented rather than
glossed. In CI a ``certify verify`` build step is unbypassable — the agent cannot
route around it, and that is the strongest claim available. Claude Code's hooks
fire deterministically, so enforcement there is strong. Cursor, Codex and
Antigravity are advisory to partial until someone has actually checked, and
nothing is listed as supported before then.

An MCP tool is not enforcement: the model chooses whether to call it. That is the
same lesson as an empty acceptance list one level up — a check the graded party
can decline is not a check.
"""
