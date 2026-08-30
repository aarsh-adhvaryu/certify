"""The verifier gate.

Deterministic checks, never an LLM judge. The verdict is also the router's
training signal, so a grader that could be wrong in the same direction as the
thing it grades would corrupt the feedback loop rather than close it.
"""

from certify.verify.base import (
    UnknownVerifier,
    Verifier,
    VerifierKind,
    VerifierRegistry,
    VerifyContext,
)
from certify.verify.pytest_gate import PytestGate
from certify.verify.stateful import (
    CommandVerifier,
    PollOutcome,
    PortVerifier,
    SuspensionPlan,
    plan_for,
    poll_until,
    port_is_open,
)
from certify.verify.static import JsonVerifier, PythonSyntaxVerifier, SchemaVerifier

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
