"""Agentic Operator — a deterministic verification layer for coding agents.

Guards, a frozen-criteria gate, a falsifiability check and a spend ledger. The
parts that decide whether work is acceptable call no model at all, which is what
makes them portable to any host and free to run.

The orchestration layer that used to sit alongside them — conductor, router,
escalation ladder, model registry, execution planes — was removed in slot 0.1 of
the pivot. It is recoverable at the ``v1-orchestrator`` tag if a decision behind
it ever needs re-reading.

``OPERATOR-v2.md`` is the design, ``PLAN.md`` the route, ``CLAUDE.md`` the memory.
"""

__version__ = "0.1.0"
