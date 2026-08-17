"""Slot 14 — record and replay.

From the first real key onward, every live interaction becomes a permanent
regression fixture. That is the mitigation for Block B's stated risk: a control
plane that is provably correct against a mock can still be fragile against a real
model, and recorded transcripts are the only way to keep real behaviour in the
test suite without paying for it twice.

**Matching is strict.** A cassette is keyed by a hash of the request — model,
messages, tools, params, everything that goes on the wire. Change a prompt and
nothing matches, and the replay fails saying so.

That failure is the feature. Prompt drift and standing-context changes are
otherwise invisible: ordered playback would hand back a response recorded for a
completely different prompt and the test would pass, proving nothing. The cost is
that editing a prompt means re-recording, which is the honest price of knowing
your fixtures still describe reality.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx

from aop.core.schemas import Strict

CASSETTE_VERSION = 1


class CassetteMiss(Exception):
    """No recorded interaction matches this request.

    Almost always means a prompt changed. Re-record rather than loosening the
    match: a green test replaying the wrong response is worse than a red one.
    """


class CassetteError(Exception):
    pass


def request_digest(body: dict[str, Any]) -> str:
    """Stable fingerprint of a request body.

    ``stream`` and ``stream_options`` are excluded so one recording serves both
    call styles — the model's answer does not depend on how it is framed on the
    wire, and requiring two recordings of the same exchange would double the
    cost of every re-record for no benefit.
    """
    trimmed = {k: v for k, v in body.items() if k not in ("stream", "stream_options")}
    canonical = json.dumps(trimmed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Interaction(Strict):
    """One recorded exchange."""

    digest: str
    model: str
    status: int = 200
    body: dict[str, Any] | None = None
    """Non-streaming JSON response."""

    sse: str | None = None
    """Raw server-sent-event text, byte-for-byte as it arrived."""

    request: dict[str, Any] | None = None
    """The originating request, stored for diagnosis of a miss. Never matched
    against — the digest is the key."""


class Cassette(Strict):
    version: int = CASSETTE_VERSION
    interactions: dict[str, Interaction] = {}

    @classmethod
    def load(cls, path: Path) -> Cassette:
        if not Path(path).is_file():
            raise CassetteError(f"cassette not found: {path}")
        cassette = cls.model_validate_json(Path(path).read_text(encoding="utf-8"))
        if cassette.version != CASSETTE_VERSION:
            raise CassetteError(
                f"cassette version {cassette.version} is not readable by this build "
                f"(expected {CASSETTE_VERSION})"
            )
        return cassette

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # sort_keys and indent keep the file diffable, so a re-record shows what
        # actually changed rather than a wall of reordered JSON.
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, indent=2)
        path.write_text(payload + "\n", encoding="utf-8", newline="\n")


class ReplayProvider:
    """Records live traffic, or replays it.

    In ``record`` mode it passes every request through to ``upstream`` and keeps
    the answer. In ``replay`` mode it never touches the network.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        mode: str = "replay",
        upstream: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if mode not in ("record", "replay"):
            raise ValueError(f"mode must be 'record' or 'replay', got {mode!r}")
        if mode == "record" and upstream is None:
            raise ValueError("record mode needs an upstream transport to record from")

        self.path = Path(path)
        self.mode = mode
        self._upstream = upstream
        self._cassette = (
            Cassette() if mode == "record" else Cassette.load(self.path)
        )
        self.misses: list[str] = []

    @property
    def cassette(self) -> Cassette:
        return self._cassette

    def __len__(self) -> int:
        return len(self._cassette.interactions)

    def save(self) -> None:
        self._cassette.save(self.path)

    def transport(self) -> httpx.AsyncBaseTransport:
        return _ReplayTransport(self)

    # -- replay ------------------------------------------------------------

    def lookup(self, body: dict[str, Any]) -> Interaction:
        digest = request_digest(body)
        try:
            return self._cassette.interactions[digest]
        except KeyError:
            self.misses.append(digest)
            raise CassetteMiss(
                f"no recorded interaction for model {body.get('model')!r} "
                f"(digest {digest[:12]}). The request differs from every recording "
                f"in {self.path.name} — usually a changed prompt. Re-record rather "
                f"than loosening the match."
            ) from None

    def store(self, body: dict[str, Any], response: httpx.Response, raw: bytes) -> None:
        digest = request_digest(body)
        is_sse = "text/event-stream" in response.headers.get("content-type", "")
        self._cassette.interactions[digest] = Interaction(
            digest=digest,
            model=body.get("model", ""),
            status=response.status_code,
            body=None if is_sse else json.loads(raw or b"null"),
            sse=raw.decode("utf-8") if is_sse else None,
            request=body,
        )


class _ReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, provider: ReplayProvider) -> None:
        self._provider = provider

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")

        if self._provider.mode == "record":
            upstream = await self._provider._upstream.handle_async_request(request)
            raw = await upstream.aread()
            await upstream.aclose()
            self._provider.store(body, upstream, raw)
            return httpx.Response(
                upstream.status_code, content=raw, headers=upstream.headers
            )

        recorded = self._provider.lookup(body)
        if recorded.sse is not None:
            return httpx.Response(
                recorded.status,
                content=recorded.sse.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(recorded.status, json=recorded.body)
