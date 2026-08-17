"""Model registry: the only path from a role slot to a model identity.

Nothing outside this package and ``config/`` may name a model. That is what
makes swapping one an edit to a TOML file rather than a code change.
"""

from aop.registry.registry import (
    MissingCredential,
    NoCapableTier,
    Registry,
    RegistryError,
    UnknownRole,
)

__all__ = [
    "MissingCredential",
    "NoCapableTier",
    "Registry",
    "RegistryError",
    "UnknownRole",
]
