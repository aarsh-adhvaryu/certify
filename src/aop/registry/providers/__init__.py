"""Providers that stand in for a real vendor during development.

Both speak OpenAI dialect over an httpx transport rather than short-circuiting
the adapter. That is deliberate: it means every mock-driven test in the codebase
exercises real request shaping, real SSE parsing, and real usage extraction, so
the control plane is not merely correct against a convenient fiction.
"""

from aop.registry.providers.mock import MockProvider, MockReply
from aop.registry.providers.replay import (
    Cassette,
    CassetteError,
    CassetteMiss,
    Interaction,
    ReplayProvider,
    request_digest,
)

__all__ = [
    "Cassette",
    "CassetteError",
    "CassetteMiss",
    "Interaction",
    "MockProvider",
    "MockReply",
    "ReplayProvider",
    "request_digest",
]
