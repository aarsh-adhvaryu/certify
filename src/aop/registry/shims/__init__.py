"""Provider shims, looked up by the ``provider`` field in ``registry.toml``.

An unknown provider raises rather than silently falling back to the baseline.
The reason is a typo: ``provider = "moonshto"`` would otherwise keep working —
because it *is* OpenAI dialect — while quietly losing whatever quirk handling the
real Moonshot shim provides, and that is not a failure anyone would notice.

A new vendor that needs no special handling does not need code: point it at
``provider = "openai"`` with its own ``base_url``. Registering a named shim is
only for vendors that actually differ.
"""

from __future__ import annotations

from aop.registry.shims.base import Shim
from aop.registry.shims.mock import MockShim


class UnknownProvider(Exception):
    pass


_SHIMS: dict[str, Shim] = {}


def register(shim: Shim) -> Shim:
    """Add a shim. Re-registering a name replaces it, which is what tests want."""
    _SHIMS[shim.name] = shim
    return shim


def shim_for(provider: str) -> Shim:
    try:
        return _SHIMS[provider]
    except KeyError:
        known = ", ".join(sorted(_SHIMS))
        raise UnknownProvider(
            f"no shim registered for provider {provider!r}; known: {known}. "
            f"A vendor needing no special handling can use provider = 'openai'."
        ) from None


def known_providers() -> tuple[str, ...]:
    return tuple(sorted(_SHIMS))


register(Shim())  # the generic OpenAI-dialect baseline
register(MockShim())

# Real vendor shims (moonshot, dashscope, deepseek) are deliberately absent.
# Writing them now would mean writing unverified code against APIs the plan
# already says must be re-checked at Slot 41, and a wrong shim that looks right
# is worse than an absent one.

__all__ = [
    "MockShim",
    "Shim",
    "UnknownProvider",
    "known_providers",
    "register",
    "shim_for",
]
