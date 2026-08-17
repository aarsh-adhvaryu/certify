"""Slot 21 — the budget guard.

Spec §7 names cost runaway as risk #1 and mitigates it with discipline. Discipline
is not a mechanism: it degrades exactly when things go wrong, which is when the
spend happens. This is the mechanism.

Two ceilings, both read from ``policy.toml`` and both compared against exact
Decimal spend summed from the attempt ledger:

* **per task** — one runaway task cannot consume the day.
* **per day** — a runaway *pattern* cannot consume the month.

Spend is recomputed from the ledger rather than kept as a running counter. A
counter that drifts from the rows is worse than no counter at all, because the
guard would then be enforcing a fiction with total confidence.

Checked **before** dispatch, not after. Finding out you are over budget once the
tokens are spent is an audit, not a guard.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from aop.core.config import BudgetPolicy
from aop.core.events import BudgetEvent, EventBus
from aop.core.ids import Clock, SystemClock
from aop.core.schemas import FailureClass
from aop.core.state import StateStore


class BudgetExceeded(Exception):
    """A ceiling was reached.

    Carries ``FailureClass.BUDGET``, which halts the task rather than retrying
    it: retrying is precisely the thing that would make it worse.
    """

    failure_class = FailureClass.BUDGET

    def __init__(self, scope: str, spent: Decimal, ceiling: Decimal) -> None:
        super().__init__(
            f"{scope} budget exhausted: ${spent} spent against a ${ceiling} ceiling"
        )
        self.scope = scope
        self.spent = spent
        self.ceiling = ceiling


class BudgetGuard:
    """Deterministic, zero-token spend ceilings."""

    def __init__(
        self,
        policy: BudgetPolicy,
        store: StateStore,
        *,
        bus: EventBus | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._policy = policy
        self._store = store
        self._bus = bus
        self._clock = clock or SystemClock()

    async def task_spend(self, task_id: str) -> Decimal:
        return await self._store.task_spend(task_id)

    async def day_spend(self, day: date | None = None) -> Decimal:
        return await self._store.spend_on(day or self._clock.now().date())

    async def check(self, task_id: str, *, projected: Decimal = Decimal("0")) -> None:
        """Raise if dispatching would breach a ceiling.

        ``projected`` is an optional estimate of what the next call will cost.
        Passing it turns the guard from "stop once over" into "stop before going
        over", which is the difference between a ceiling and a receipt.
        """
        task = await self.task_spend(task_id) + projected
        if task >= self._policy.per_task_usd:
            self._publish("task", task, self._policy.per_task_usd, task_id)
            raise BudgetExceeded("per-task", task, self._policy.per_task_usd)

        day = await self.day_spend() + projected
        if day >= self._policy.per_day_usd:
            self._publish("day", day, self._policy.per_day_usd, task_id)
            raise BudgetExceeded("per-day", day, self._policy.per_day_usd)

    async def remaining(self, task_id: str) -> Decimal:
        """Headroom before the tighter of the two ceilings bites."""
        task_left = self._policy.per_task_usd - await self.task_spend(task_id)
        day_left = self._policy.per_day_usd - await self.day_spend()
        return max(min(task_left, day_left), Decimal("0"))

    def _publish(
        self, scope: str, spent: Decimal, ceiling: Decimal, task_id: str
    ) -> None:
        if self._bus:
            self._bus.emit(
                BudgetEvent,
                task_id=task_id,
                scope=scope,
                spent_usd=spent,
                ceiling_usd=ceiling,
                halted=True,
            )
