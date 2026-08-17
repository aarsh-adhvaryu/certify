"""Slot 15 — the tool-call protocol.

Two things carry this slot: the loop terminates, and nothing the model does badly
turns into a crash. Every failure mode below has to come back as a message the
model can read and correct itself from.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel

from aop.core.config import load_settings
from aop.core.schemas import Role
from aop.registry import Registry
from aop.registry.adapter import Adapter, Message, ToolCall
from aop.registry.providers import MockProvider, MockReply
from aop.registry.toolcalls import ToolBox, run_tools

PROJECT_CONFIG = Path(__file__).resolve().parent / "config"
ASK = [Message.user("read a.py then tell me what it does")]


@pytest.fixture
def registry() -> Registry:
    return Registry(load_settings(PROJECT_CONFIG).registry, env={})


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
async def adapter(registry, provider):
    a = Adapter(registry, transport=provider.transport())
    yield a
    await a.aclose()


@pytest.fixture
def box() -> ToolBox:
    tb = ToolBox()
    tb.register(
        "read_file",
        lambda path: f"contents of {path}",
        description="Read a file",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    return tb


def _call(name="read_file", arguments='{"path": "a.py"}') -> ToolCall:
    return ToolCall(id="call_0", name=name, arguments=arguments)


# ------------------------------------------------------- schema emission


def test_schema_is_openai_shaped(box):
    schema = box.schemas()[0]
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "read_file"
    assert schema["function"]["parameters"]["required"] == ["path"]


def test_schemas_are_ordered_deterministically(box):
    box.register("write_file", lambda path, body: "ok")
    box.register("apply_patch", lambda diff: "ok")
    assert [s["function"]["name"] for s in box.schemas()] == [
        "apply_patch",
        "read_file",
        "write_file",
    ]


def test_decorator_registration_uses_the_docstring():
    box = ToolBox()

    @box.tool()
    def list_dir(path: str):
        """List a directory."""
        return []

    assert "list_dir" in box
    assert box.schemas()[0]["function"]["description"] == "List a directory."


def test_pydantic_model_becomes_the_schema():
    class Args(BaseModel):
        path: str
        limit: int = 10

    box = ToolBox()
    box.register("read", lambda path, limit: "", parameters=Args)
    props = box.schemas()[0]["function"]["parameters"]["properties"]
    assert set(props) == {"path", "limit"}


def test_a_tool_with_no_parameters_still_emits_a_valid_schema():
    box = ToolBox()
    box.register("ping", lambda: "pong")
    assert box.schemas()[0]["function"]["parameters"] == {
        "type": "object",
        "properties": {},
    }


# ------------------------------------------------------------- dispatch


async def test_dispatch_runs_the_handler(box):
    assert await box.dispatch(_call()) == "contents of a.py"


async def test_async_handlers_are_awaited():
    box = ToolBox()

    async def fetch(url: str):
        return f"fetched {url}"

    box.register("fetch", fetch)
    assert await box.dispatch(_call("fetch", '{"url": "x"}')) == "fetched x"


async def test_non_string_results_are_serialised(box):
    box.register("stats", lambda: {"files": 3, "lines": 120})
    assert json.loads(await box.dispatch(_call("stats", "{}"))) == {"files": 3, "lines": 120}


async def test_unserialisable_result_still_returns_something(box):
    box.register("weird", lambda: object())
    assert isinstance(await box.dispatch(_call("weird", "{}")), str)


async def test_pydantic_validation_runs_before_the_handler():
    class Args(BaseModel):
        count: int

    seen = []
    box = ToolBox()
    box.register("count", lambda count: seen.append(count) or "ok", parameters=Args)

    await box.dispatch(_call("count", '{"count": "7"}'))
    assert seen == [7]  # coerced by the model, not passed through as a string


# ------------------------------- nothing the model does badly is a crash


async def test_unknown_tool_comes_back_as_a_message(box):
    result = json.loads(await box.dispatch(_call("delete_everything", "{}")))
    assert result["error"] == "unknown_tool"
    assert "read_file" in result["detail"]


async def test_malformed_json_arguments_come_back_as_a_message(box):
    """Models get this wrong often enough that a crash would make the system
    brittle for entirely ordinary reasons."""
    result = json.loads(await box.dispatch(_call(arguments="{not json")))
    assert result["error"] == "bad_arguments"


async def test_non_object_arguments_come_back_as_a_message(box):
    result = json.loads(await box.dispatch(_call(arguments='["a", "b"]')))
    assert result["error"] == "bad_arguments"


async def test_wrong_argument_names_come_back_as_a_message(box):
    result = json.loads(await box.dispatch(_call(arguments='{"filename": "a.py"}')))
    assert result["error"] == "bad_arguments"


async def test_schema_violation_comes_back_readable():
    class Args(BaseModel):
        count: int

    box = ToolBox()
    box.register("count", lambda count: "ok", parameters=Args)
    result = json.loads(await box.dispatch(_call("count", '{"count": "many"}')))
    assert result["error"] == "bad_arguments"
    assert "count" in result["detail"]


async def test_a_raising_handler_does_not_kill_the_task(box):
    """This is the shape the guard layer needs in Slot 30: a denial is a cheap
    message on the same tier, never an escalation."""
    def explode(path: str):
        raise PermissionError("outside the jail root")

    box.register("read_file", explode)
    result = json.loads(await box.dispatch(_call()))
    assert result["error"] == "tool_failed"
    assert "outside the jail root" in result["detail"]


async def test_empty_arguments_are_treated_as_an_empty_object(box):
    box.register("ping", lambda: "pong")
    assert await box.dispatch(ToolCall(id="c", name="ping", arguments="")) == "pong"


# ------------------------------------------------------------- the loop


async def test_loop_runs_a_tool_then_answers(adapter, provider, box):
    provider.script(
        "mock-low",
        MockReply(
            finish_reason="tool_calls",
            tool_calls=[{"name": "read_file", "arguments": '{"path": "a.py"}'}],
        ),
        MockReply(content="it prints a number"),
    )
    result = await run_tools(adapter, Role.LOW, ASK, box)

    assert result.response.content == "it prints a number"
    assert result.iterations == 2
    assert not result.exhausted


async def test_tool_result_is_fed_back_to_the_model(adapter, provider, box):
    provider.script(
        "mock-low",
        MockReply(
            finish_reason="tool_calls",
            tool_calls=[{"name": "read_file", "arguments": '{"path": "a.py"}'}],
        ),
        MockReply(content="done"),
    )
    result = await run_tools(adapter, Role.LOW, ASK, box)

    tool_messages = [m for m in result.messages if m.role == "tool"]
    assert tool_messages[0].content == "contents of a.py"
    # And the model actually saw it on the second call.
    assert any(
        m.get("role") == "tool" for m in provider.calls[1]["messages"]
    )


async def test_several_tool_calls_in_one_turn(adapter, provider, box):
    provider.script(
        "mock-low",
        MockReply(
            finish_reason="tool_calls",
            tool_calls=[
                {"name": "read_file", "arguments": '{"path": "a.py"}'},
                {"name": "read_file", "arguments": '{"path": "b.py"}'},
            ],
        ),
        MockReply(content="compared them"),
    )
    result = await run_tools(adapter, Role.LOW, ASK, box)
    assert [m.content for m in result.messages if m.role == "tool"] == [
        "contents of a.py",
        "contents of b.py",
    ]


async def test_a_model_that_never_converges_is_capped(adapter, provider, box):
    """Otherwise it burns money in a loop the verifier never gets to judge."""
    provider.script(
        "mock-low",
        MockReply(
            finish_reason="tool_calls",
            tool_calls=[{"name": "read_file", "arguments": '{"path": "a.py"}'}],
        ),
    )
    result = await run_tools(adapter, Role.LOW, ASK, box, max_iterations=3)

    assert result.exhausted
    assert result.iterations == 3


async def test_exhaustion_is_returned_not_raised(adapter, provider, box):
    """Whether a non-converging loop is a failure, a retry, or a hand-back is
    the orchestrator's policy call, not this module's."""
    provider.script(
        "mock-low",
        MockReply(
            finish_reason="tool_calls",
            tool_calls=[{"name": "read_file", "arguments": "{}"}],
        ),
    )
    result = await run_tools(adapter, Role.LOW, ASK, box, max_iterations=2)
    assert result.exhausted
    assert result.messages  # partial work is preserved, not thrown away


async def test_usage_and_cost_accumulate_across_the_loop(adapter, provider, box):
    """The budget guard needs the whole exchange, not just the last call."""
    provider.script(
        "mock-low",
        MockReply(
            finish_reason="tool_calls",
            tokens_in=100,
            tokens_out=10,
            tool_calls=[{"name": "read_file", "arguments": '{"path": "a.py"}'}],
        ),
        MockReply(content="done", tokens_in=200, tokens_out=20),
    )
    result = await run_tools(adapter, Role.LOW, ASK, box)

    assert result.usage.tokens_in == 300
    assert result.usage.tokens_out == 30
    assert result.cost_usd == Decimal("0")  # mock registry is free


async def test_no_tools_means_a_single_call(adapter, provider):
    provider.script("mock-low", MockReply(content="just an answer"))
    result = await run_tools(adapter, Role.LOW, ASK, ToolBox())

    assert result.iterations == 1
    assert "tools" not in provider.calls[0]


async def test_a_bad_tool_call_lets_the_model_recover(adapter, provider, box):
    """The whole reason errors are messages: the model gets a second chance."""
    provider.script(
        "mock-low",
        MockReply(
            finish_reason="tool_calls",
            tool_calls=[{"name": "raed_file", "arguments": "{}"}],
        ),
        MockReply(
            finish_reason="tool_calls",
            tool_calls=[{"name": "read_file", "arguments": '{"path": "a.py"}'}],
        ),
        MockReply(content="recovered"),
    )
    result = await run_tools(adapter, Role.LOW, ASK, box)

    assert result.response.content == "recovered"
    assert not result.exhausted
    errors = [m.content for m in result.messages if m.role == "tool" and "error" in m.content]
    assert json.loads(errors[0])["error"] == "unknown_tool"


async def test_loop_streams_by_default(registry, provider, box):
    """A task that spends thirty seconds in a tool loop behind a blank screen is
    one a user kills."""
    async with Adapter(registry, transport=provider.transport()) as a:
        provider.script("mock-low", MockReply(content="answered"))
        await run_tools(a, Role.LOW, ASK, box)
    assert provider.calls[0]["stream"] is True


async def test_streaming_can_be_turned_off(adapter, provider, box):
    provider.script("mock-low", MockReply(content="answered"))
    await run_tools(adapter, Role.LOW, ASK, box, stream=False)
    assert "stream" not in provider.calls[0]
