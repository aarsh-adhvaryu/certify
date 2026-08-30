"""Deterministic, zero-token guards.

No guard on any path here makes a model call. A guard that costs tokens is not a
guard — it is a second opinion, and it can be talked out of its answer.

A denial is returned as a structured result, never raised through the worker's
tool loop as a crash: it becomes a cheap tool message on the same tier with the
cached prefix intact (see ``registry/toolcalls.py``), and it never escalates.
"""

from certify.guards.budget import BudgetExceeded, BudgetGuard
from certify.guards.commands import CommandGuard
from certify.guards.denial import GuardDenied
from certify.guards.discovery import DiscoveryScope, Found
from certify.guards.pathjail import PathJail

__all__ = [
    "BudgetExceeded",
    "BudgetGuard",
    "CommandGuard",
    "DiscoveryScope",
    "Found",
    "GuardDenied",
    "PathJail",
]
