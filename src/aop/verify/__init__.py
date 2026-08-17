"""The verifier gate.

Deterministic checks, never an LLM judge. The verdict is also the router's
training signal, so a grader that could be wrong in the same direction as the
thing it grades would corrupt the feedback loop rather than close it.
"""

from aop.verify.base import (
    UnknownVerifier,
    Verifier,
    VerifierKind,
    VerifierRegistry,
    VerifyContext,
)
from aop.verify.pytest_gate import PytestGate
from aop.verify.stateful import (
    CommandVerifier,
    PollOutcome,
    PortVerifier,
    SuspensionPlan,
    plan_for,
    poll_until,
    port_is_open,
)
from aop.verify.static import JsonVerifier, PythonSyntaxVerifier, SchemaVerifier

__all__ = [
    "CommandVerifier",
    "JsonVerifier",
    "PollOutcome",
    "PortVerifier",
    "PytestGate",
    "PythonSyntaxVerifier",
    "SchemaVerifier",
    "SuspensionPlan",
    "UnknownVerifier",
    "Verifier",
    "VerifierKind",
    "VerifierRegistry",
    "VerifyContext",
    "plan_for",
    "poll_until",
    "port_is_open",
]
