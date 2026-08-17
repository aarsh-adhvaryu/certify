"""Slot 72, moved up — running a suite and comparing candidates.

This is the thing that turns "swap the battery" into "swap and auto-validate"
(spec §3.1). Given a suite and a registry config, it runs every task through the
real pipeline and records what the **verifier gate** said, what it cost, and how
long it took. Given two such runs, it says which allocation won and by how much.

Three rules it exists to enforce:

**The gate grades, not a judge.** Pass-rate here is the same verdict the
orchestrator acts on. A separate scoring model would be a second opinion that can
be talked out of its answer, and it would drift from the thing it is meant to
predict.

**Candidates are compared on identical fixtures.** Each task is staged into a
freshly emptied workspace, so the second candidate does not inherit the first
one's leftovers.

**Cost is reported next to pass-rate, always.** A model that wins on pass-rate at
six times the price has not obviously won, and a comparison that hides the price
invites exactly that mistake.
"""

from __future__ import annotations

import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import Field

from aop.core.config import Settings
from aop.core.ids import Clock, SystemClock
from aop.core.schemas import Role, Strict, TaskStatus
from aop.evals.suite import EvalSuite, EvalTask
from aop.operator import Operator


class TaskResult(Strict):
    task_id: str
    passed: bool
    expected_pass: bool = True
    status: str = ""
    attempts: int = 0
    cost_usd: Decimal = Decimal("0")
    seconds: float = 0.0
    role_chosen: Role | None = None
    difficulty_expected: str = ""
    error: str | None = None

    @property
    def as_expected(self) -> bool:
        """Whether the outcome matched what the suite predicted.

        Not the same as ``passed``: a task the suite expects to be refused counts
        as a success when it *is* refused."""
        return self.passed == self.expected_pass


class RunReport(Strict):
    """One suite, one allocation."""

    label: str
    suite: str
    started_at: datetime
    results: list[TaskResult] = Field(default_factory=list)
    roles: dict[str, str] = Field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def correct(self) -> int:
        return sum(1 for r in self.results if r.as_expected)

    @property
    def pass_rate(self) -> float:
        return (self.correct / self.total) if self.total else 0.0

    @property
    def cost(self) -> Decimal:
        return sum((r.cost_usd for r in self.results), Decimal("0"))

    @property
    def seconds(self) -> float:
        return sum(r.seconds for r in self.results)

    @property
    def attempts(self) -> int:
        return sum(r.attempts for r in self.results)

    @property
    def cost_per_task(self) -> Decimal:
        return (self.cost / self.total) if self.total else Decimal("0")

    def describe(self) -> str:
        lines = [
            f"  {self.label}",
            f"    suite      {self.suite}  ({self.total} tasks)",
            f"    correct    {self.correct}/{self.total}  ({self.pass_rate:.0%})",
            f"    cost       ${self.cost}  (${self.cost_per_task:.4f}/task)",
            f"    attempts   {self.attempts}  ({self.attempts / self.total:.1f}/task)"
            if self.total else "",
            f"    wall clock {self.seconds:.1f}s",
        ]
        roles = ", ".join(f"{k}={v}" for k, v in sorted(self.roles.items()))
        lines.append(f"    roles      {roles}")
        return "\n".join(line for line in lines if line)

    def failures(self) -> list[TaskResult]:
        return [r for r in self.results if not r.as_expected]


class Comparison(Strict):
    """Two allocations, same suite."""

    incumbent: RunReport
    candidate: RunReport

    @property
    def pass_rate_delta(self) -> float:
        return self.candidate.pass_rate - self.incumbent.pass_rate

    @property
    def cost_ratio(self) -> float:
        if not self.incumbent.cost:
            return 1.0
        return float(self.candidate.cost / self.incumbent.cost)

    @property
    def promote(self) -> bool:
        """Whether the candidate should replace the incumbent.

        Deliberately conservative: it must not lose accuracy. A cheaper model
        that solves fewer tasks is not a saving — the failures come back as retries,
        escalations and your attention, none of which appear on the invoice.
        """
        return self.pass_rate_delta >= 0 and self.candidate.cost <= self.incumbent.cost

    def verdict(self) -> str:
        if self.promote:
            return (
                f"PROMOTE — same or better pass-rate "
                f"({self.candidate.pass_rate:.0%} vs {self.incumbent.pass_rate:.0%}) "
                f"at {self.cost_ratio:.2f}x the cost"
            )
        if self.pass_rate_delta < 0:
            return (
                f"KEEP INCUMBENT — candidate solved fewer tasks "
                f"({self.candidate.pass_rate:.0%} vs {self.incumbent.pass_rate:.0%})"
            )
        return (
            f"KEEP INCUMBENT — candidate matched on pass-rate but costs "
            f"{self.cost_ratio:.2f}x"
        )

    def describe(self) -> str:
        return "\n".join(
            [
                self.incumbent.describe(),
                "",
                self.candidate.describe(),
                "",
                f"  {self.verdict()}",
            ]
        )


class Harness:
    """Runs a suite against one allocation."""

    def __init__(
        self,
        suite: EvalSuite,
        settings: Settings,
        *,
        label: str = "run",
        clock: Clock | None = None,
        transport=None,
        gate=None,
    ) -> None:
        self.suite = suite
        self.settings = settings
        self.label = label
        self._clock = clock or SystemClock()
        self._transport = transport
        self._gate = gate

    async def run(self, tasks: list[EvalTask] | None = None) -> RunReport:
        chosen = tasks if tasks is not None else self.suite.tasks
        report = RunReport(
            label=self.label,
            suite=self.suite.name,
            started_at=self._clock.now(),
            roles={
                role.value: self.settings.registry.roles[role].model_id for role in Role
            },
        )
        for task in chosen:
            report.results.append(await self._run_one(task))
        return report

    async def _run_one(self, task: EvalTask) -> TaskResult:
        started = time.monotonic()
        operator: Operator | None = None
        try:
            # Staging is inside the guard on purpose. A single task with a
            # mistyped fixture would otherwise abort the whole run, losing every
            # task after it — an expensive way to discover a typo.
            self.suite.stage(task, self.settings.jail_root)

            operator = Operator(
                self.settings, transport=self._transport, clock=self._clock, gate=self._gate
            )
            await operator.start()
            settled = await operator.run_directive(
                task.directive, timeout=task.timeout_seconds
            )
            attempts = await operator.store.list_attempts(settled.task_id)
            return TaskResult(
                task_id=task.id,
                passed=settled.status is TaskStatus.DONE,
                expected_pass=task.expect_pass,
                status=settled.status.value,
                attempts=len(attempts),
                cost_usd=settled.cost_usd,
                seconds=time.monotonic() - started,
                role_chosen=attempts[-1].role if attempts else None,
                difficulty_expected=task.difficulty.value,
            )
        except Exception as exc:  # noqa: BLE001 - one bad task must not end the run
            return TaskResult(
                task_id=task.id,
                passed=False,
                expected_pass=task.expect_pass,
                status="error",
                seconds=time.monotonic() - started,
                difficulty_expected=task.difficulty.value,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if operator is not None:
                await operator.stop()


async def compare(
    suite: EvalSuite,
    incumbent: Settings,
    candidate: Settings,
    *,
    incumbent_label: str = "incumbent",
    candidate_label: str = "candidate",
    clock: Clock | None = None,
    transport=None,
    gate=None,
) -> Comparison:
    """Run both allocations over the same suite and say which won."""
    return Comparison(
        incumbent=await Harness(
            suite, incumbent, label=incumbent_label, clock=clock,
            transport=transport, gate=gate,
        ).run(),
        candidate=await Harness(
            suite, candidate, label=candidate_label, clock=clock,
            transport=transport, gate=gate,
        ).run(),
    )
