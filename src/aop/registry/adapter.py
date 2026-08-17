"""Slots 10 & 11 — the adapter.

One HTTP client drives every model, because they all speak the OpenAI-compatible
dialect. Per-provider differences go in shims (Slot 12), never here.

The adapter is the only place that knows a model is reached over a network. It
takes a role, not a model — resolution happens in the registry — and it returns a
priced response, because a call whose cost is unknown is a call the budget guard
cannot see.

**Streaming and non-streaming must agree.** Two code paths that assemble a
response differently will drift, and the drift shows up as "it works unless you
stream", which is miserable to debug. ``test_adapter.py`` asserts they produce
identical content for the same scripted reply.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Sequence
from decimal import Decimal
from typing import Any

import httpx

from aop.core.events import EventBus, TokenEmitted
from aop.core.ids import Clock, SystemClock
from aop.core.schemas import ReasoningEffort, Role, Strict
from aop.registry.cost import CostModel, Usage
from aop.registry.registry import Registry
from aop.registry.shims import shim_for

DEFAULT_TIMEOUT = 120.0


class AdapterError(Exception):
    """Base for every adapter failure. All of these map to
    ``FailureClass.TRANSPORT``: they say nothing about whether the tier was
    capable, so they must never escalate or become a training label."""


class ProviderError(AdapterError):
    """The provider answered with an error status."""

    def __init__(self, status: int, body: str, model_id: str) -> None:
        super().__init__(f"{model_id} returned HTTP {status}: {body[:400]}")
        self.status = status
        self.body = body


class TransportError(AdapterError):
    """The request never got an answer — timeout, DNS, connection reset."""


class MalformedResponse(AdapterError):
    """The provider answered, but not in the shape the dialect requires."""


# --------------------------------------------------------------------------
# Wire types
# --------------------------------------------------------------------------


class ToolCall(Strict):
    """A model's request to run a tool.

    ``arguments`` stays the raw JSON string the wire carried. Parsing it is the
    tool layer's job (Slot 15), and it is allowed to be malformed — models get
    this wrong often enough that it has to be a handled case, not an exception.
    """

    id: str
    name: str
    arguments: str = "{}"


class Message(Strict):
    role: str
    """``system`` | ``user`` | ``assistant`` | ``tool``."""

    content: str | None = None
    tool_calls: list[ToolCall] = []
    tool_call_id: str | None = None
    name: str | None = None

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            out["content"] = self.content
        if self.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            out["name"] = self.name
        return out

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str | None = None, **kw: Any) -> Message:
        return cls(role="assistant", content=content, **kw)

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> Message:
        return cls(role="tool", tool_call_id=tool_call_id, content=content)


class ChatResponse(Strict):
    role: Role
    model_id: str
    content: str = ""
    reasoning: str | None = None
    """Thinking-block text where the provider exposes it separately.

    Kept apart from ``content`` because it renders differently and, on the
    conductor, is billed as output at the highest rate in the system."""

    tool_calls: list[ToolCall] = []
    finish_reason: str | None = None
    usage: Usage
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def as_message(self) -> Message:
        """The assistant turn to append before running tools."""
        return Message(role="assistant", content=self.content or None, tool_calls=self.tool_calls)


class StreamChunk(Strict):
    """One delta off the wire."""

    text: str = ""
    reasoning: str = ""
    finish_reason: str | None = None


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------


def _tool_calls_from(raw: Iterable[dict[str, Any]] | None) -> list[ToolCall]:
    calls = []
    for item in raw or []:
        fn = item.get("function") or {}
        calls.append(
            ToolCall(
                id=item.get("id") or f"call_{len(calls)}",
                name=fn.get("name") or "",
                arguments=fn.get("arguments") or "{}",
            )
        )
    return calls


def _reasoning_from(message: dict[str, Any]) -> str | None:
    """Best-effort read of a separate thinking field.

    Providers disagree on the key and none of ours is verified yet, so both
    common spellings are accepted and neither is required.
    """
    for key in ("reasoning_content", "reasoning"):
        value = message.get(key)
        if value:
            return str(value)
    return None


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------


class Adapter:
    """Drives every provider through one client."""

    def __init__(
        self,
        registry: Registry,
        *,
        cost_model: CostModel | None = None,
        bus: EventBus | None = None,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        mounts: dict[str, httpx.AsyncBaseTransport] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._registry = registry
        self._cost = cost_model or CostModel(registry)
        self._bus = bus
        self._clock = clock or SystemClock()
        # ``mounts`` routes by host, so a registry mixing a real provider with the
        # mock works without a second client. A single ``transport`` still
        # overrides everything, which is what tests want.
        self._client = httpx.AsyncClient(
            transport=transport, mounts=mounts or {}, timeout=timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Adapter:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- request building --------------------------------------------------

    def build_body(
        self,
        role: Role | str,
        messages: Sequence[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        stream: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Assemble the request body. Exposed so tests and the replay provider
        can hash exactly what would be sent."""
        entry = self._registry.entry(role)
        body: dict[str, Any] = {
            "model": entry.model_id,
            "messages": [m.to_wire() for m in messages],
            **entry.params,
        }
        if tools:
            body["tools"] = tools
        if reasoning_effort is not None:
            body["reasoning_effort"] = reasoning_effort.value
        if stream:
            body["stream"] = True
            # Without this, streamed responses carry no usage block at all and
            # the budget guard is blind. Slot 09 raises rather than costing zero,
            # so forgetting it fails loudly instead of silently.
            body["stream_options"] = {"include_usage": True}
        if extra:
            body.update(extra)
        return shim_for(entry.provider).prepare_body(body, entry)

    def _headers(self, role: Role | str) -> dict[str, str]:
        entry = self._registry.entry(role)
        base = {"Content-Type": "application/json"}
        return shim_for(entry.provider).prepare_headers(
            base, entry, self._registry.api_key(role)
        )

    def _url(self, role: Role | str) -> str:
        return f"{self._registry.entry(role).base_url}/chat/completions"

    # -- non-streaming (Slot 10) -------------------------------------------

    async def complete(
        self,
        role: Role | str,
        messages: Sequence[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResponse:
        entry = self._registry.entry(role)
        body = self.build_body(
            role, messages, tools=tools, reasoning_effort=reasoning_effort, extra=extra
        )
        started = self._clock.now()

        try:
            response = await self._client.post(
                self._url(role), json=body, headers=self._headers(role)
            )
        except httpx.HTTPError as exc:
            raise TransportError(f"{entry.model_id}: {exc!r}") from exc

        if response.status_code >= 400:
            raise ProviderError(response.status_code, response.text, entry.model_id)

        try:
            payload = shim_for(entry.provider).normalise_response(response.json())
        except json.JSONDecodeError as exc:
            raise MalformedResponse(f"{entry.model_id}: response was not JSON") from exc

        return self._to_response(role, payload, started)

    def _to_response(
        self, role: Role | str, payload: dict[str, Any], started: Any
    ) -> ChatResponse:
        choices = payload.get("choices")
        if not choices:
            raise MalformedResponse(f"response carried no choices: {sorted(payload)}")
        choice = choices[0]
        message = choice.get("message") or {}

        usage = Usage.from_payload(payload.get("usage"))
        elapsed = self._clock.now() - started
        return ChatResponse(
            role=self._registry._coerce(role),
            model_id=self._registry.model_id(role),
            content=message.get("content") or "",
            reasoning=_reasoning_from(message),
            tool_calls=_tool_calls_from(message.get("tool_calls")),
            finish_reason=choice.get("finish_reason"),
            usage=usage,
            cost_usd=self._cost.cost(role, usage),
            latency_ms=int(elapsed.total_seconds() * 1000),
        )

    # -- streaming (Slot 11) -----------------------------------------------

    async def stream(
        self,
        role: Role | str,
        messages: Sequence[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        extra: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Yield deltas as they arrive, publishing each to the bus.

        The final aggregated response is available from :attr:`last_response`
        once the iterator is exhausted, because usage only arrives in the last
        frame and cannot be known before then.
        """
        entry = self._registry.entry(role)
        resolved = self._registry._coerce(role)
        body = self.build_body(
            role,
            messages,
            tools=tools,
            reasoning_effort=reasoning_effort,
            stream=True,
            extra=extra,
        )
        started = self._clock.now()
        shim = shim_for(entry.provider)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_fragments: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage_payload: dict[str, Any] | None = None

        try:
            async with self._client.stream(
                "POST", self._url(role), json=body, headers=self._headers(role)
            ) as response:
                if response.status_code >= 400:
                    text = (await response.aread()).decode("utf-8", "replace")
                    raise ProviderError(response.status_code, text, entry.model_id)

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = shim.normalise_chunk(json.loads(data))
                    except json.JSONDecodeError as exc:
                        raise MalformedResponse(
                            f"{entry.model_id}: unparseable stream frame {data[:120]!r}"
                        ) from exc

                    if chunk.get("usage"):
                        usage_payload = chunk["usage"]

                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]

                        text = delta.get("content") or ""
                        think = ""
                        for key in ("reasoning_content", "reasoning"):
                            if delta.get(key):
                                think = str(delta[key])
                                break

                        for fragment in delta.get("tool_calls") or []:
                            self._accumulate_tool_call(tool_fragments, fragment)

                        if text:
                            content_parts.append(text)
                        if think:
                            reasoning_parts.append(think)

                        if (text or think) and self._bus:
                            self._bus.emit(
                                TokenEmitted,
                                task_id=task_id,
                                role=resolved,
                                text=text or think,
                                reasoning=not text and bool(think),
                            )
                        if text or think or choice.get("finish_reason"):
                            yield StreamChunk(
                                text=text,
                                reasoning=think,
                                finish_reason=choice.get("finish_reason"),
                            )
        except httpx.HTTPError as exc:
            raise TransportError(f"{entry.model_id}: {exc!r}") from exc

        usage = Usage.from_payload(usage_payload)
        elapsed = self._clock.now() - started
        self.last_response = ChatResponse(
            role=resolved,
            model_id=entry.model_id,
            content="".join(content_parts),
            reasoning="".join(reasoning_parts) or None,
            tool_calls=[
                ToolCall(
                    id=frag.get("id") or f"call_{idx}",
                    name=(frag.get("function") or {}).get("name") or "",
                    arguments=(frag.get("function") or {}).get("arguments") or "{}",
                )
                for idx, frag in sorted(tool_fragments.items())
            ],
            finish_reason=finish_reason,
            usage=usage,
            cost_usd=self._cost.cost(role, usage),
            latency_ms=int(elapsed.total_seconds() * 1000),
        )

    @staticmethod
    def _accumulate_tool_call(
        store: dict[int, dict[str, Any]], fragment: dict[str, Any]
    ) -> None:
        """Reassemble a tool call split across frames.

        Providers stream ``arguments`` a few characters at a time, so the JSON is
        only valid once every fragment for that index has arrived.
        """
        idx = fragment.get("index", 0)
        slot = store.setdefault(idx, {"function": {"name": "", "arguments": ""}})
        if fragment.get("id"):
            slot["id"] = fragment["id"]
        fn = fragment.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]

    async def complete_streaming(
        self,
        role: Role | str,
        messages: Sequence[Message],
        **kwargs: Any,
    ) -> ChatResponse:
        """Stream to the bus, but return the same aggregate as :meth:`complete`.

        This is the method callers should normally use: the UI gets live tokens,
        the orchestrator gets one priced response, and there is no second
        assembly path to drift out of step.
        """
        async for _ in self.stream(role, messages, **kwargs):
            pass
        return self.last_response
