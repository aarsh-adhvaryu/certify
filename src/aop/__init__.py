"""Agentic Operator — a deterministic verification layer for coding agents.

Guards, a frozen-test gate, a falsifiability check and a spend ledger. The parts
that decide whether work is acceptable call no model at all, which is what makes
them portable to any host and free to run.

An orchestration layer — conductor, router, escalation ladder — exists alongside
them and is optional.

``CLAUDE.md`` is the design, the invariants and the plan of action.
"""

__version__ = "0.1.0"
