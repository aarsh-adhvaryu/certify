"""Slot 15 — the tool-call protocol.

Turns Python callables into tool schemas, parses what the model asks for, runs
it, and feeds the result back until the model stops asking. This is the wire
protocol only; the actual guarded tool *surface* — filesystem, shell — arrives in
Slot 30 and plugs in here.

The governing rule: **a tool problem is a message, not an exception.** An unknown
tool, malformed JSON arguments, a handler that raises — all of them come back as
a structured tool result the model can read and correct itself from. Models get
this wrong often enough that treating it as a crash would make the system
brittle for entirely ordinary reasons.

That is also the shape the guard layer needs. When a path-jail denial arrives in
Slot 30, it will travel this same path: a cheap structured message appended to
the volatile tail, same tier, cache intact — never an escalation.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ValidationError

from aop.core.schemas import ReasoningEffort, Role, Strict
from aop.registry.adapter import Adapter, ChatResponse, Message, ToolCall
from aop.registry.cost import Usage

DEFAULT_MAX_ITERATIONS = 12


class Tool(Strict):
    """One callable the model may invoke."""

    name: str
    description: str = ""
    parameters: dict[str, Any] = {}
    """JSON Schema for the arguments."""

    handler: Callable[..., Any]
    model: type[BaseModel] | None = None
    """When present, arguments are validated against it before the handler runs
    and validation failures go back to the model as a readable message."""

    model_config = {"arbitrary_types_allowed": True, "extra": "forbid"}

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


def _error(kind: str, message: str) -> str:
    """Structured, machine-readable, and greppable in a transcript.

    JSON rather than prose so the model gets an unambiguous signal, and so a
    later pass can count error kinds without parsing English.
    """
    return json.dumps({"error": kind, "detail": message})


class ToolBox:
    """A named collection of tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def register(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        description: str = "",
        parameters: dict[str, Any] | type[BaseModel] | None = None,
    ) -> Tool:
        model: type[BaseModel] | None = None
        if isinstance(parameters, type) and issubclass(parameters, BaseModel):
            model = parameters
            schema = model.model_json_schema()
        else:
            schema = parameters or {"type": "object", "properties": {}}

        tool = Tool(
            name=name,
            description=description,
            parameters=schema,
            handler=handler,
            model=model,
        )
        self._tools[name] = tool
        return tool

    def tool(
        self,
        name: str | None = None,
        *,
        description: str = "",
        parameters: dict[str, Any] | type[BaseModel] | None = None,
    ):
        """Decorator form."""

        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(
                name or fn.__name__,
                fn,
                description=description or (fn.__doc__ or "").strip(),
                parameters=parameters,
            )
            return fn

        return decorate

    def schemas(self) -> list[dict[str, Any]]:
        return [self._tools[n].schema() for n in sorted(self._tools)]

    async def dispatch(self, call: ToolCall) -> str:
        """Run one tool call and return the content for its tool message.

        Never raises for anything the model did. See the module docstring.
        """
        tool = self._tools.get(call.name)
        if tool is None:
            return _error(
                "unknown_tool",
                f"no tool named {call.name!r}; available: {', '.join(self.names()) or 'none'}",
            )

        try:
            arguments = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
            return _error("bad_arguments", f"arguments were not valid JSON: {exc}")

        if not isinstance(arguments, dict):
            return _error("bad_arguments", "arguments must be a JSON object")

        if tool.model is not None:
            try:
                arguments = tool.model.model_validate(arguments).model_dump()
            except ValidationError as exc:
                return _error("bad_arguments", exc.json())

        try:
            result = tool.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
        except TypeError as exc:
            # Wrong argument names reach the handler as a TypeError. That is the
            # model's mistake, so it gets told rather than the task dying.
            return _error("bad_arguments", str(exc))
        except Exception as exc:  # noqa: BLE001 - deliberate: see module docstring
            return _error("tool_failed", f"{type(exc).__name__}: {exc}")

        return _stringify(result)


def _stringify(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result)
    except (TypeError, ValueError):
        return str(result)


class ToolRunResult(Strict):
    """The outcome of a full tool-calling exchange."""

    response: ChatResponse
    messages: list[Message]
    """The full conversation including assistant turns and tool results, ready
    to be appended to."""

    iterations: int
    usage: Usage
    cost_usd: Decimal

    exhausted: bool = False
    """True when the iteration cap stopped the loop rather than the model.

    Returned rather than raised: whether a non-converging loop is a failure, a
    retry, or a hand-back is the orchestrator's policy call, not this module's.
    """


async def run_tools(
    adapter: Adapter,
    role: Role | str,
    messages: Sequence[Message],
    toolbox: ToolBox,
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
    reasoning_effort: ReasoningEffort | None = None,
    task_id: str | None = None,
    stream: bool = True,
) -> ToolRunResult:
    """Call the model, run whatever tools it asks for, repeat until it stops.

    Streams by default so the UI has something to render — a task that spends
    thirty seconds in a tool loop with a blank screen is one a user kills.
    """
    conversation = list(messages)
    schemas = toolbox.schemas() or None
    total = Usage()
    cost = Decimal("0")
    response: ChatResponse | None = None

    for iteration in range(1, max_iterations + 1):
        if stream:
            response = await adapter.complete_streaming(
                role,
                conversation,
                tools=schemas,
                reasoning_effort=reasoning_effort,
                task_id=task_id,
            )
        else:
            response = await adapter.complete(
                role, conversation, tools=schemas, reasoning_effort=reasoning_effort
            )

        total = total + response.usage
        cost += response.cost_usd
        conversation.append(response.as_message())

        if not response.wants_tools:
            return ToolRunResult(
                response=response,
                messages=conversation,
                iterations=iteration,
                usage=total,
                cost_usd=cost,
            )

        for call in response.tool_calls:
            conversation.append(
                Message.tool_result(call.id, await toolbox.dispatch(call))
            )

    return ToolRunResult(
        response=response,
        messages=conversation,
        iterations=max_iterations,
        usage=total,
        cost_usd=cost,
        exhausted=True,
    )
