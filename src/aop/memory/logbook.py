"""Slot 32 — the logbook.

One row per **attempt**, never per task. That shape is the point: when a task
fails at ``low`` and passes at ``high``, one piece of work produces two labelled
rows — evidence about a tier the system did not end up using. The escalation
ladder is therefore not just error recovery, it is the router's data-generating
process, and without it the router would only ever learn about tiers it already
chose.

Eligibility is decided by ``FailureClass`` and filtered at the read
(``StateStore.training_rows``), not here and not by the caller. Guard trips,
transport faults, and budget halts are all recorded — they are useful for
debugging and for the journal — but none of them is a statement about whether the
tier was capable, so none of them is a label.

Every row pins both ``schema_version`` and ``spec_schema_version``. The task spec
is the conductor-to-worker contract and it will change; without the pin, a format
change silently corrupts the training set rather than invalidating it visibly.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from aop.core.events import AttemptFinished, EventBus
from aop.core.ids import Clock, IdSource, SystemClock, UuidIds
from aop.core.schemas import Attempt, FailureClass, Role, TaskSpec, Verdict, VerdictStatus
from aop.core.state import StateStore
from aop.registry.cost import Usage


class Logbook:
    """Writes attempt rows and answers questions about them."""

    def __init__(
        self,
        store: StateStore,
        *,
        bus: EventBus | None = None,
        clock: Clock | None = None,
        ids: IdSource | None = None,
    ) -> None:
        self._store = store
        self._bus = bus
        self._clock = clock or SystemClock()
        self._ids = ids or UuidIds()

    def new_attempt_id(self) -> str:
        return self._ids.new_id("attempt")

    async def record(
        self,
        *,
        task_id: str,
        spec: TaskSpec,
        role: Role,
        model_id: str,
        verdict: Verdict,
        usage: Usage,
        cost_usd: Decimal,
        started_at: datetime,
        index: int | None = None,
        attempt_id: str | None = None,
        latency_ms: int = 0,
        features: dict[str, float] | None = None,
        billable: bool = True,
    ) -> Attempt:
        """Append one attempt.

        ``features`` is snapshotted as it was at dispatch, not recomputed later.
        Recomputing would make retraining silently retroactive: change the
        extractor and every historical row would quietly describe a decision that
        was never actually made that way.
        """
        attempt = Attempt(
            attempt_id=attempt_id or self.new_attempt_id(),
            task_id=task_id,
            spec_id=spec.spec_id,
            spec_schema_version=spec.schema_version,
            index=index if index is not None else await self._store.next_attempt_index(task_id),
            role=role,
            model_id=model_id,
            verdict=verdict.status,
            failure_class=verdict.failure_class,
            failure_reason=verdict.reason,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cost_usd=cost_usd,
            billable=billable,
            latency_ms=latency_ms,
            started_at=started_at,
            ended_at=self._clock.now(),
            features=features or {},
        )
        await self._store.record_attempt(attempt)

        if self._bus:
            self._bus.emit(
                AttemptFinished,
                task_id=task_id,
                attempt_id=attempt.attempt_id,
                role=role,
                verdict=verdict.status,
                failure_class=verdict.failure_class,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
            )
        return attempt

    # -- reading -----------------------------------------------------------

    async def attempts(self, task_id: str) -> list[Attempt]:
        return await self._store.list_attempts(task_id)

    async def training_rows(self, *, limit: int | None = None) -> list[Attempt]:
        """Rows eligible as router labels. Filtered at the read."""
        return await self._store.training_rows(limit=limit)

    async def verifier_failures_at(self, task_id: str, role: Role) -> int:
        """How many times the *verifier* has rejected work at this tier.

        Counts only verifier failures, which is what the escalation counter is
        allowed to see. A guard trip at this tier does not bring escalation any
        closer, because it says nothing about whether the tier was strong enough.
        """
        return sum(
            1
            for a in await self._store.list_attempts(task_id)
            if a.role is role and a.failure_class is FailureClass.VERIFIER
        )

    async def tier_stats(self) -> dict[Role, dict[str, int]]:
        """Pass and fail counts per tier, over eligible rows only.

        The first thing worth looking at once real traffic exists: if ``low``
        never fails, the router is being too cautious; if it always fails, the
        cheap tier is not earning its place.
        """
        stats: dict[Role, dict[str, int]] = {}
        for attempt in await self._store.training_rows():
            bucket = stats.setdefault(attempt.role, {"pass": 0, "fail": 0})
            key = "pass" if attempt.verdict is VerdictStatus.PASS else "fail"
            bucket[key] += 1
        return stats
