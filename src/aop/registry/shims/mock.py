"""Shim for the in-process mock provider.

The mock speaks strict OpenAI dialect over an httpx transport, so almost nothing
differs. The one thing it does not have is a credential.
"""

from __future__ import annotations

from typing import Any

from aop.core.config import ModelEntry
from aop.registry.shims.base import Shim


class MockShim(Shim):
    name = "mock"

    def prepare_headers(
        self,
        headers: dict[str, str],
        entry: ModelEntry,
        api_key: str | None,
    ) -> dict[str, str]:
        """No auth header. A mock that demanded a key would make it impossible
        to notice a missing one at the point it actually starts to matter."""
        return dict(headers)

    def prepare_body(self, body: dict[str, Any], entry: ModelEntry) -> dict[str, Any]:
        """Keep ``reasoning_effort`` when the mock's capability tag claims it.

        Inherited behaviour already does this; stated here because the mock is
        the only place where the capability tags are fiction we chose, and tests
        for effort routing depend on them being honoured.
        """
        return super().prepare_body(body, entry)
