"""Slot 25 — stateful verification.

Some checks take seconds to minutes: a server binding to a port, ``npm install``
finishing, an endpoint answering 200. The rule for all of them is the same.

**Never let a model wait.** The orchestrator starts the work, then polls
mechanically. No tokens are spent while polling, and no context is held open.
Handing a model a "check if it's up yet" loop would burn the context window on
the least valuable thing it could possibly be doing.

**Suspension is a threshold, not an absolute.** A check expected to finish in
under ``suspend_threshold_seconds`` is awaited inline, because serialising a task
to SQLite and reviving it costs more than the wait does. Past the threshold the
task is parked (Slot 06) and holds nothing.

The poller is deliberately dumb: a predicate, an interval, a deadline. Anything
cleverer would be something else that can be wrong.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

from certify.backends.base import RunBackend
from certify.core.config import VerifyPolicy
from certify.core.schemas import Strict, Verdict
from certify.verify.base import Verifier, VerifierKind, VerifyContext

Probe = Callable[[], bool | Awaitable[bool]]


class PollOutcome(Strict):
    ready: bool
    waited_ms: int
    attempts: int


async def poll_until(
    probe: Probe,
    *,
    interval: float = 0.5,
    timeout: float = 30.0,
) -> PollOutcome:
    """Call ``probe`` until it returns True or the deadline passes.

    Mechanical by design — no model, no tokens, no judgement. The only decisions
    are how often and for how long, and both come from policy.
    """
    started = time.monotonic()
    deadline = started + timeout
    attempts = 0

    while True:
        attempts += 1
        result = probe()
        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
            result = await result
        if result:
            return PollOutcome(
                ready=True,
                waited_ms=int((time.monotonic() - started) * 1000),
                attempts=attempts,
            )
        if time.monotonic() >= deadline:
            return PollOutcome(
                ready=False,
                waited_ms=int((time.monotonic() - started) * 1000),
                attempts=attempts,
            )
        await asyncio.sleep(min(interval, max(deadline - time.monotonic(), 0)))


def port_is_open(host: str, port: int, *, connect_timeout: float = 0.25) -> bool:
    """Cheapest possible liveness probe: can we open a TCP connection."""
    try:
        with socket.create_connection((host, port), timeout=connect_timeout):
            return True
    except OSError:
        return False


class PortVerifier(Verifier):
    """Wait for something to listen on a port."""

    name = "port"
    kind = VerifierKind.STATEFUL

    def __init__(
        self,
        port: int,
        *,
        host: str = "127.0.0.1",
        policy: VerifyPolicy | None = None,
    ) -> None:
        self._port = port
        self._host = host
        self._policy = policy or VerifyPolicy()
        self.expected_seconds = self._policy.poll_timeout_seconds

    async def verify(self, ctx: VerifyContext) -> Verdict:
        outcome = await poll_until(
            lambda: port_is_open(self._host, self._port),
            interval=self._policy.poll_interval_seconds,
            timeout=self._policy.poll_timeout_seconds,
        )
        if outcome.ready:
            return Verdict.passed(
                "port", duration_ms=outcome.waited_ms, port=str(self._port)
            )
        return Verdict.failed(
            "port",
            reason=(
                f"nothing was listening on {self._host}:{self._port} after "
                f"{self._policy.poll_timeout_seconds:g}s ({outcome.attempts} probes)"
            ),
            duration_ms=outcome.waited_ms,
        )


class CommandVerifier(Verifier):
    """Run a command and require a zero exit.

    For the ``npm install`` shape of check: long, one-shot, and either it worked
    or it did not.
    """

    name = "command"
    kind = VerifierKind.STATEFUL

    def __init__(
        self,
        backend: RunBackend,
        argv: list[str],
        *,
        timeout: float = 300.0,
        name: str | None = None,
    ) -> None:
        self._backend = backend
        self._argv = argv
        self._timeout = timeout
        self.expected_seconds = timeout
        if name:
            self.name = name

    async def verify(self, ctx: VerifyContext) -> Verdict:
        try:
            result = await self._backend.run(self._argv, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - a guard denial is not a model failure
            return Verdict.errored(self.name, f"could not run {self._argv[0]}: {exc}")

        if result.timed_out:
            return Verdict.errored(
                self.name,
                f"{' '.join(self._argv)} did not finish within {self._timeout:g}s",
                duration_ms=result.duration_ms,
            )
        if result.ok:
            return Verdict.passed(self.name, duration_ms=result.duration_ms)
        return Verdict.failed(
            self.name,
            reason=f"{' '.join(self._argv)} exited {result.exit_code}\n\n{result.output}",
            duration_ms=result.duration_ms,
        )


class SuspensionPlan(Strict):
    """What the orchestrator should do about a slow check."""

    suspend: bool
    wait: timedelta
    reason: str


def plan_for(verifier: Verifier, policy: VerifyPolicy) -> SuspensionPlan:
    """Decide between awaiting inline and parking the task.

    Below the threshold, serialising to SQLite and reviving costs more than the
    wait does. Above it, holding a task in memory means a crash loses work that
    was already paid for.
    """
    if verifier.kind is not VerifierKind.STATEFUL:
        return SuspensionPlan(
            suspend=False, wait=timedelta(0), reason="static check runs inline"
        )

    expected = verifier.expected_seconds or policy.poll_timeout_seconds
    if expected < policy.suspend_threshold_seconds:
        return SuspensionPlan(
            suspend=False,
            wait=timedelta(seconds=expected),
            reason=(
                f"{verifier.name} expected to take {expected:g}s, under the "
                f"{policy.suspend_threshold_seconds:g}s threshold"
            ),
        )
    return SuspensionPlan(
        suspend=True,
        wait=timedelta(seconds=expected),
        reason=f"waiting on {verifier.name} (~{expected:g}s)",
    )
