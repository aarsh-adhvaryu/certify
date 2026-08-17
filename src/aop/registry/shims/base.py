"""Slot 12 — the provider shim seam.

Every model in this system speaks the OpenAI-compatible dialect, so one adapter
drives all of them. Shims exist for the small places that differ: a thinking-
budget knob, a JSON-mode flag, an extra header, a response field in the wrong
place.

A shim is deliberately narrow — four hooks, each with a working default. That
keeps provider quirks from leaking into the adapter, where they would accumulate
into a pile of ``if provider == ...`` branches that nobody dares delete.
"""

from __future__ import annotations

from typing import Any

from aop.core.config import ModelEntry


class Shim:
    """Baseline behaviour: pure OpenAI dialect, bearer-token auth.

    Subclass and override only what actually differs. Every hook returns a new
    object rather than mutating in place, so a shim cannot accidentally corrupt
    a request that is about to be retried.
    """

    name = "openai"

    def prepare_body(self, body: dict[str, Any], entry: ModelEntry) -> dict[str, Any]:
        """Adjust the request body before it goes out.

        The default drops ``reasoning_effort`` when the registry says this model
        has no such knob. Sending it anyway is at best ignored and at worst a
        rejected request, and the caller should not have to check first.
        """
        body = dict(body)
        if not entry.capabilities.reasoning_effort:
            body.pop("reasoning_effort", None)
        return body

    def prepare_headers(
        self,
        headers: dict[str, str],
        entry: ModelEntry,
        api_key: str | None,
    ) -> dict[str, str]:
        headers = dict(headers)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def normalise_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Bring a response back to plain OpenAI shape."""
        return payload

    def normalise_chunk(self, chunk: dict[str, Any]) -> dict[str, Any]:
        """Same, for one streamed chunk."""
        return chunk
