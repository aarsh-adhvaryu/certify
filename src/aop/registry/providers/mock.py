"""Slot 13 — the mock provider.

An in-process HTTP transport that answers in OpenAI dialect. Nothing here
short-circuits the adapter: requests are shaped, serialised, parsed, and streamed
exactly as they would be against a real vendor.

That costs a little more effort than returning Python objects, and buys the thing
the plan names as Block B's main risk — a control plane that is provably correct
against a fake and fragile against reality. Everything between the orchestrator
and the socket is real here.

Two behaviours are faithful on purpose rather than convenient:

* **Usage appears only when ``stream_options.include_usage`` was sent.** Forget
  the flag and the mock reproduces the real blindness, and Slot 09 raises. A mock
  that helpfully always reported usage would hide the exact bug that flag exists
  to prevent.
* **Streamed content arrives in fragments**, including tool-call arguments split
  mid-JSON, because that is how providers actually behave and reassembly is
  where the bugs live.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import httpx

from aop.core.schemas import Strict


class MockReply(Strict):
    """One scripted answer."""

    content: str = ""
    reasoning: str | None = None

    tool_calls: list[dict[str, str]] = []
    """Each ``{"name": ..., "arguments": <json string>}``."""

    finish_reason: str = "stop"

    tokens_in: int | None = None
    tokens_out: int | None = None
    """Derived deterministically from the payload when left unset."""

    cached_in: int = 0

    status: int = 200
    error: str | None = None
    """Set together with a 4xx/5xx ``status`` to exercise the transport path."""

    omit_usage: bool = False
    """Answer without a usage block even when asked for one — the only way to
    test that a blind budget guard fails loudly rather than costing zero."""


def _estimate(text: str) -> int:
    """Rough token count. Deterministic, which is the only property that matters
    here — a mock's job is to be reproducible, not to be a tokeniser."""
    return max(len(text) // 4, 1) if text else 0


def _canonical(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


_DIRECTIVE = re.compile(r"<Directive>\s*(.*?)\s*</Directive>", re.DOTALL)


class MockProvider:
    """Scripted, deterministic answers over a real HTTP transport."""

    def __init__(self, default: MockReply | None = None, *, plausible: bool = False) -> None:
        self._scripts: dict[str, list[MockReply]] = {}
        self._default = default
        self._plausible = plausible
        self.calls: list[dict[str, Any]] = []
        """Every request body received, in order — for asserting what was sent."""

    # -- scripting ---------------------------------------------------------

    def script(self, model_id: str, *replies: MockReply) -> MockProvider:
        """Queue answers for a model.

        Consumed in order; the last one repeats once the queue is exhausted. That
        makes both "fail twice then succeed" and "always answer X" natural to
        express, which is what the escalation-ladder tests need.
        """
        self._scripts.setdefault(model_id, []).extend(replies)
        return self

    def reply_for(self, body: dict[str, Any]) -> MockReply:
        model_id = body.get("model", "")
        queue = self._scripts.get(model_id)
        if queue:
            return queue.pop(0) if len(queue) > 1 else queue[0]
        if self._default is not None:
            return self._default
        if self._plausible:
            structured = self._protocol_reply(body)
            if structured is not None:
                return structured
        return self._derived(body)

    @staticmethod
    def _protocol_reply(body: dict[str, Any]) -> MockReply | None:
        """Answer requests where the protocol demands a *shape*, not just text.

        Only one place needs this today: the conductor must return a task spec as
        JSON, and a mock that cannot produce protocol-valid structured output
        cannot exercise the pipeline past step two. This is the same principle
        that makes the mock speak real HTTP and real SSE — being faithful to the
        contract rather than convenient — applied one level up.

        Deliberately narrow. It keys off the immutable ``<Directive>`` block,
        which the context assembler always emits, and returns None for anything
        it does not recognise so ordinary calls stay dumb and deterministic.
        """
        messages = body.get("messages") or []
        system = " ".join(
            str(m.get("content") or "") for m in messages if m.get("role") == "system"
        )
        match = _DIRECTIVE.search(system)
        if match is None or body.get("tools"):
            return None

        directive = match.group(1).strip()
        return MockReply(
            content=json.dumps(
                {
                    "goal": directive,
                    "acceptance": [f"the change satisfies: {directive}"],
                    "constraints": ["do not change unrelated behaviour"],
                    "difficulty_hint": "medium",
                },
                indent=2,
            )
        )

    @staticmethod
    def _derived(body: dict[str, Any]) -> MockReply:
        """A stable answer derived from the request.

        Same input, same output, forever — which is what makes an unscripted mock
        safe to assert against.
        """
        digest = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
        return MockReply(content=f"mock reply {digest[:16]}")

    # -- transport ---------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.calls.append(body)
        reply = self.reply_for(body)

        if reply.status >= 400:
            return httpx.Response(
                reply.status,
                json={"error": {"message": reply.error or "mock failure"}},
            )

        usage = self._usage_for(body, reply)
        if body.get("stream"):
            wants_usage = bool((body.get("stream_options") or {}).get("include_usage"))
            return httpx.Response(
                200,
                content=self._sse(reply, usage if wants_usage else None),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(200, json=self._json(body, reply, usage))

    def _usage_for(self, body: dict[str, Any], reply: MockReply) -> dict[str, Any] | None:
        if reply.omit_usage:
            return None
        prompt = sum(
            _estimate(str(m.get("content") or "")) for m in body.get("messages") or []
        )
        tokens_in = reply.tokens_in if reply.tokens_in is not None else max(prompt, 1)
        tokens_out = (
            reply.tokens_out
            if reply.tokens_out is not None
            else _estimate(reply.content) + _estimate(reply.reasoning or "")
        )
        usage: dict[str, Any] = {
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "total_tokens": tokens_in + tokens_out,
        }
        if reply.cached_in:
            usage["prompt_tokens_details"] = {"cached_tokens": reply.cached_in}
        return usage

    def _message(self, reply: MockReply) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": reply.content or None}
        if reply.reasoning:
            message["reasoning_content"] = reply.reasoning
        if reply.tool_calls:
            message["tool_calls"] = [
                {
                    "id": f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": tc.get("arguments", "{}"),
                    },
                }
                for i, tc in enumerate(reply.tool_calls)
            ]
        return message

    def _json(
        self, body: dict[str, Any], reply: MockReply, usage: dict[str, Any] | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "model": body.get("model", ""),
            "choices": [
                {
                    "index": 0,
                    "message": self._message(reply),
                    "finish_reason": reply.finish_reason,
                }
            ],
        }
        if usage is not None:
            payload["usage"] = usage
        return payload

    def _sse(self, reply: MockReply, usage: dict[str, Any] | None) -> bytes:
        """Render the reply as server-sent events.

        Content is split into small deltas and tool-call arguments are cut
        mid-JSON, because reassembly is where streaming bugs actually live and a
        mock that emitted everything in one frame would never exercise it.
        """
        frames: list[dict[str, Any]] = [
            {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
        ]

        for piece in _split(reply.reasoning or ""):
            frames.append(
                {"choices": [{"index": 0, "delta": {"reasoning_content": piece}, "finish_reason": None}]}
            )
        for piece in _split(reply.content):
            frames.append(
                {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
            )

        for i, call in enumerate(reply.tool_calls):
            frames.append(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": i,
                                        "id": f"call_{i}",
                                        "type": "function",
                                        "function": {"name": call["name"], "arguments": ""},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                }
            )
            for piece in _split(call.get("arguments", "{}"), size=5):
                frames.append(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {"index": i, "function": {"arguments": piece}}
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    }
                )

        frames.append(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": reply.finish_reason}]}
        )
        if usage is not None:
            frames.append({"choices": [], "usage": usage})

        lines = "".join(f"data: {json.dumps(f)}\n\n" for f in frames)
        return (lines + "data: [DONE]\n\n").encode("utf-8")


def _split(text: str, size: int = 7) -> list[str]:
    if not text:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]
