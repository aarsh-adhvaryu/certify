"""Slots 41 & 42 — the local service and the overlay.

The tests that carry these slots: a submitted directive runs the whole pipeline,
the socket carries what actually happened, and a client that stops reading is
told it fell behind rather than being quietly lied to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aop.core.config import load_settings
from aop.core.ids import FrozenClock, SequentialIds
from aop.core.schemas import TaskStatus, Verdict
from aop.operator import Operator
from aop.registry.providers import MockProvider, MockReply
from aop.service import build_app
from aop.verify.base import Verifier, VerifierKind, VerifierRegistry

PROJECT_CONFIG = Path(__file__).resolve().parent / "config"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

GOOD_SPEC = json.dumps({
    "goal": "add exponential backoff to the uploader",
    "acceptance": ["retries exactly three times"],
    "artifacts": ["src/uploader.py"],
})


class _Gate(VerifierRegistry):
    """A scripted gate, so a test can choose the trajectory it wants to watch."""

    def __init__(self, *verdicts: Verdict) -> None:
        super().__init__()
        outer = self

        class Scripted(Verifier):
            name = "pytest"
            kind = VerifierKind.STATIC

            async def verify(self, ctx):
                return outer._verdicts.pop(0) if len(outer._verdicts) > 1 else outer._verdicts[0]

        self._verdicts = list(verdicts)
        self.register(Scripted())


@pytest.fixture
def provider() -> MockProvider:
    p = MockProvider()
    p.script("mock-conductor", MockReply(content=GOOD_SPEC))
    p.script("mock-low", MockReply(content="tests written"))
    p.script("mock-high", MockReply(content="implemented"))
    return p


def _operator(tmp_path, provider, *verdicts) -> Operator:
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    return Operator(
        settings,
        transport=provider.transport(),
        clock=FrozenClock(T0, step=timedelta(milliseconds=250)),
        ids=SequentialIds(),
        gate=_Gate(*(verdicts or (Verdict.passed("pytest"),))),
    )


@pytest.fixture
def client(tmp_path, provider):
    with TestClient(build_app(_operator(tmp_path, provider))) as c:
        yield c


# ------------------------------------------------------------------ basics


def test_health(client):
    assert client.get("/health").json()["ok"] is True


def test_overlay_is_served(client):
    body = client.get("/").text
    assert "<title>Operator</title>" in body


def test_overlay_needs_no_network(client):
    """It has to render with the network unplugged, and later inside a frameless
    webview with no browser chrome."""
    body = client.get("/").text
    assert "http://" not in body.replace("ws://${location.host}", "")
    assert "cdn" not in body.lower()
    assert "<script src" not in body


def test_snapshot_describes_the_running_system(client):
    snap = client.get("/api/snapshot").json()
    assert set(snap["roles"]) == {"conductor", "low", "high", "max"}
    assert snap["backend"] == "windows"
    assert snap["tasks"] == []


def test_snapshot_reports_the_mock_registry_as_free(client):
    """Nothing spends until Slot 41's decisions are taken."""
    snap = client.get("/api/snapshot").json()
    assert all(role["free"] for role in snap["roles"].values())


def test_unknown_task_is_404(client):
    assert client.get("/api/tasks/nope").status_code == 404


def test_empty_directive_is_rejected(client):
    assert client.post("/api/tasks", json={"directive": ""}).status_code == 422


# ------------------------------------------------------- the whole pipeline


def test_a_directive_runs_end_to_end(tmp_path, provider):
    """Submit → plan → route → author tests → ladder → verdict → done."""
    with TestClient(build_app(_operator(tmp_path, provider))) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.receive_json()["kind"] == "snapshot"

            response = client.post("/api/tasks", json={"directive": "add backoff to the uploader"})
            assert response.status_code == 202
            task_id = response.json()["task_id"]

            kinds = []
            for _ in range(60):
                message = ws.receive_json()
                kinds.append(message.get("kind"))
                if message.get("kind") == "task_status" and message.get("status") == "done":
                    break

        assert "routed" in kinds
        assert "attempt_finished" in kinds
        detail = client.get(f"/api/tasks/{task_id}").json()
        assert detail["task"]["status"] == TaskStatus.DONE.value
        assert len(detail["attempts"]) == 1


def test_submission_returns_before_the_work_finishes(client):
    """A directive that blocked until done would make the UI feel dead for the
    whole run; progress is reported on the socket instead."""
    response = client.post("/api/tasks", json={"directive": "do something slow"})
    assert response.status_code == 202
    assert response.json()["status"] == TaskStatus.PENDING.value


def test_a_failing_task_climbs_and_hands_to_a_human(tmp_path, provider):
    for model in ("mock-low", "mock-high", "mock-max"):
        provider.script(model, MockReply(content="attempt"))

    operator = _operator(tmp_path, provider, Verdict.failed("pytest", reason="1 failed"))
    with TestClient(build_app(operator)) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            task_id = client.post("/api/tasks", json={"directive": "fix the thing"}).json()["task_id"]

            statuses = []
            for _ in range(120):
                message = ws.receive_json()
                if message.get("kind") == "task_status":
                    statuses.append(message["status"])
                    if message["status"] in ("awaiting_human", "failed"):
                        break

        assert statuses[-1] == TaskStatus.AWAITING_HUMAN.value
        attempts = client.get(f"/api/tasks/{task_id}").json()["attempts"]
        assert len(attempts) == 4  # the cap from policy.toml


# --------------------------------------------------------------- the socket


def test_socket_opens_with_a_snapshot(client):
    """So a tab that connects mid-run renders the world rather than a blank pane."""
    with client.websocket_connect("/ws") as ws:
        message = ws.receive_json()
        assert message["kind"] == "snapshot"
        assert "roles" in message["data"]


def test_events_carry_their_kind_for_client_dispatch(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        client.post("/api/tasks", json={"directive": "say hello"})
        assert ws.receive_json()["kind"] == "task_created"


def test_two_clients_both_see_everything(client):
    """The overlay is one subscriber among any number — a browser tab is a fully
    working fallback if the frameless window misbehaves."""
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        a.receive_json()
        b.receive_json()
        client.post("/api/tasks", json={"directive": "watched by two"})
        assert a.receive_json()["kind"] == "task_created"
        assert b.receive_json()["kind"] == "task_created"


def test_the_ui_is_only_a_subscriber(tmp_path, provider):
    """Nothing about a task depends on anyone watching. This is what lets the
    overlay be restarted without disturbing work in flight."""
    operator = _operator(tmp_path, provider)
    with TestClient(build_app(operator)) as client:
        task_id = client.post("/api/tasks", json={"directive": "unwatched work"}).json()["task_id"]
        for _ in range(200):
            detail = client.get(f"/api/tasks/{task_id}").json()
            if detail["task"]["status"] != TaskStatus.PENDING.value:
                break
        assert client.get("/health").json()["ok"] is True


# ------------------------------------------------------------- the journal


def test_journal_is_reachable_without_a_database(client):
    """The point of the failsafe is being legible when other things are broken."""
    client.post("/api/tasks", json={"directive": "leave a trace"})
    for _ in range(200):
        body = client.get("/api/journal").json()
        if "leave a trace" in body["markdown"]:
            break
    assert "No model wrote" in body["markdown"]


async def test_the_journal_is_read_only_to_workers(tmp_path, provider):
    """It sits in the jail so it is readable, but recovery parses it — a worker
    able to edit it could mark itself complete instead of doing the work."""
    from aop.guards import GuardDenied

    operator = _operator(tmp_path, provider)
    async with operator:
        assert operator.jail.resolve("OPERATOR.md")  # readable
        with pytest.raises(GuardDenied, match="frozen"):
            operator.jail.resolve_for_write("OPERATOR.md")


def test_journal_written_to_the_jail_root(tmp_path, provider):
    operator = _operator(tmp_path, provider)
    with TestClient(build_app(operator)) as client:
        client.get("/api/journal")
        assert operator.journal.path == tmp_path / "workspace" / "OPERATOR.md"


# --------------------------------------------------- config honoured for real


def test_mock_provider_is_wired_without_an_injected_transport(tmp_path):
    """Regression: `provider = "mock"` must mean *mock* outside the tests too.

    Every test above injects a transport, so all of them passed while the daemon
    itself tried to resolve the mock's host over the network and died on its
    first call. Only running it caught this.
    """
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    operator = Operator(settings, clock=FrozenClock(T0), ids=SequentialIds())

    assert operator.mock is not None
    assert operator.adapter._client._mounts, "no transport mounted for the mock provider"


async def test_a_directive_completes_with_no_transport_injected(tmp_path):
    """The end-to-end path the CLI actually takes."""
    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    operator = Operator(
        settings, clock=FrozenClock(T0, step=timedelta(milliseconds=50)),
        ids=SequentialIds(), gate=_Gate(Verdict.passed("pytest")),
    )
    await operator.start(run_scheduler=False)   # we drive run() ourselves
    try:
        task = await operator.lifecycle.create("add exponential backoff to the uploader")
        outcome = await operator.run(task.task_id)
    finally:
        await operator.stop()

    assert outcome.spec is not None
    assert "backoff" in outcome.spec.goal
    assert outcome.action.value == "proceed"


async def test_a_broken_environment_does_not_escalate_tiers(tmp_path):
    """Observed live: with no pytest installed the gate errors rather than fails.

    A broken tool is not a weak model. If this classed as VERIFIER the task would
    climb to the most expensive tier and write four bogus training labels while
    doing it.
    """
    from aop.core.schemas import FailureClass

    settings = load_settings(PROJECT_CONFIG, project_root=tmp_path)
    operator = Operator(
        settings, clock=FrozenClock(T0, step=timedelta(milliseconds=50)),
        ids=SequentialIds(),
        gate=_Gate(Verdict.errored("pytest", "No module named pytest")),
    )
    await operator.start(run_scheduler=False)   # we drive run() ourselves
    try:
        task = await operator.lifecycle.create("add exponential backoff to the uploader")
        await operator.run(task.task_id)
        attempts = await operator.store.list_attempts(task.task_id)

        assert len({a.role for a in attempts}) == 1, "a broken environment escalated a tier"
        assert all(a.failure_class is FailureClass.TRANSPORT for a in attempts)
        assert await operator.store.training_rows() == []
    finally:
        await operator.stop()


# ---------------------------------------------------------------- recovery


def test_orphans_are_reclaimed_on_start(tmp_path, provider):
    """A daemon that runs for days will be killed mid-task eventually."""
    operator = _operator(tmp_path, provider)
    with TestClient(build_app(operator)) as client:
        client.post("/api/tasks", json={"directive": "interrupted work"})

    second = _operator(tmp_path, provider)
    with TestClient(build_app(second)) as client:
        for task in client.get("/api/tasks").json():
            assert task["status"] != TaskStatus.RUNNING.value
