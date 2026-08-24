"""Slot 05 — durable state.

SQLite is the single source of truth for tasks and attempts. Three things depend
on that being durable rather than in-memory:

* A task parked on a stateful verifier must hold no context and survive a daemon
  restart (Slot 06).
* The attempt log is the router's training set, so losing rows loses the only
  signal the system generates about itself.
* The journal (Slot 07) is a projection of this state, which is what lets it be
  deterministic instead of an accumulation of whatever events happened to arrive.

Two storage conventions, both about exactness:

* **Money is TEXT, never REAL.** Decimal round-trips through a string unchanged;
  through a float it does not. Budget ceilings are compared against these values.
* **Timestamps are ISO-8601 TEXT, always timezone-aware UTC.** Python's sqlite3
  datetime adapters are deprecated as of 3.12, and implicit conversion is exactly
  the sort of thing that silently drops a timezone.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite

from aop.core.ids import Clock, IdSource, SystemClock, UuidIds
from aop.core.schemas import (
    Attempt,
    FailureClass,
    Role,
    Task,
    TaskSpec,
    TaskStatus,
    VerdictStatus,
    hash_directive,
)

SCHEMA_VERSION = 5


class StateError(Exception):
    """Raised on a state operation that cannot be satisfied."""


class TaskNotFound(StateError):
    pass


# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------

#: Ordered migrations. Each entry is applied inside a transaction, and
#: ``PRAGMA user_version`` records how far we got. Appending a new entry is the
#: only supported way to change the schema — editing an existing one would leave
#: already-migrated databases silently inconsistent with fresh ones.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE tasks (
            task_id          TEXT PRIMARY KEY,
            directive        TEXT NOT NULL,
            directive_hash   TEXT NOT NULL,
            status           TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            spec_json        TEXT,
            attempt_count    INTEGER NOT NULL DEFAULT 0,
            cost_usd         TEXT NOT NULL DEFAULT '0',
            suspended_reason TEXT,
            resume_after     TEXT,
            note             TEXT
        );

        CREATE INDEX idx_tasks_status ON tasks(status);
        CREATE INDEX idx_tasks_resume ON tasks(resume_after)
            WHERE resume_after IS NOT NULL;

        CREATE TABLE attempts (
            attempt_id          TEXT PRIMARY KEY,
            task_id             TEXT NOT NULL
                                REFERENCES tasks(task_id) ON DELETE CASCADE,
            spec_id             TEXT NOT NULL,
            schema_version      INTEGER NOT NULL,
            spec_schema_version INTEGER NOT NULL,
            idx                 INTEGER NOT NULL,
            role                TEXT NOT NULL,
            model_id            TEXT NOT NULL,
            verdict             TEXT NOT NULL,
            failure_class       TEXT NOT NULL,
            failure_reason      TEXT,
            tokens_in           INTEGER NOT NULL DEFAULT 0,
            tokens_out          INTEGER NOT NULL DEFAULT 0,
            cost_usd            TEXT NOT NULL DEFAULT '0',
            latency_ms          INTEGER NOT NULL DEFAULT 0,
            started_at          TEXT NOT NULL,
            ended_at            TEXT,
            features_json       TEXT NOT NULL DEFAULT '{}',
            UNIQUE(task_id, idx)
        );

        CREATE INDEX idx_attempts_task ON attempts(task_id, idx);
        CREATE INDEX idx_attempts_started ON attempts(started_at);
        """,
    ),
    (
        2,
        """
        -- Every model call that costs money, whatever it was for.
        --
        -- Budget used to be summed from `attempts`, which meant it only ever saw
        -- execution. The conductor's planning call and the test-author's call
        -- were invisible: unrecorded, unpriced, and outside the ceiling. That is
        -- precisely backwards, because the conductor is the component the spec
        -- names as cost risk #1.
        --
        -- Kept separate from `attempts` rather than folded into it: `attempts`
        -- is the router's training set, and a planning call is not evidence
        -- about whether a tier could do the work.
        CREATE TABLE spend (
            spend_id   TEXT PRIMARY KEY,
            task_id    TEXT REFERENCES tasks(task_id) ON DELETE CASCADE,
            purpose    TEXT NOT NULL,
            role       TEXT NOT NULL,
            model_id   TEXT NOT NULL,
            tokens_in  INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            cached_in  INTEGER NOT NULL DEFAULT 0,
            cost_usd   TEXT NOT NULL DEFAULT '0',
            at         TEXT NOT NULL
        );

        CREATE INDEX idx_spend_task ON spend(task_id);
        CREATE INDEX idx_spend_at ON spend(at);
        """,
    ),
    (
        3,
        """
        -- WHY a task ended, not merely that it did.
        --
        -- Every other layer already carries this: `attempts.failure_class` is
        -- what stops a guard trip escalating a tier or training the router. The
        -- task record had no equivalent, so a run killed by a dead socket was
        -- indistinguishable from one the model got wrong — and the eval harness
        -- scored them the same, reporting a network outage as a 45% pass rate.
        --
        -- Nullable: only a terminal failure has a class, and tasks that end well
        -- have nothing to say here.
        ALTER TABLE tasks ADD COLUMN failure_class TEXT;
        """,
    ),
    (
        4,
        """
        -- Whether this call actually costs money.
        --
        -- A flat-rate plane still reports a list-equivalent price, and recording
        -- it is right: the ledger should go quiet at flat rate, not blind. But
        -- the budget guard reads this table, so counting a shadow price against
        -- a dollar ceiling halts a run that has spent nothing. That happened:
        -- $0.51 of "spend" against a $1.00/day ceiling, of which $0.49 was
        -- imaginary, on a Pro subscription with no API key anywhere.
        --
        -- So: record everything, charge only what bills.
        ALTER TABLE spend ADD COLUMN billable INTEGER NOT NULL DEFAULT 1;
        """,
    ),
    (
        5,
        """
        -- The router's training set, removed with the router.
        --
        -- The 22 Aug audit settled it on evidence rather than argument: the
        -- conductor emitted difficulty_hint = "medium" for 13 specs out of 13,
        -- and that field drove the two largest weights. A classifier trained on
        -- this column would have been fitting a constant.
        --
        -- Appended rather than edited into migration 1, because editing an
        -- applied migration leaves already-migrated databases silently
        -- inconsistent with fresh ones.
        ALTER TABLE attempts DROP COLUMN features_json;
        """,
    ),
)


# --------------------------------------------------------------------------
# Conversion helpers
# --------------------------------------------------------------------------


def _dt_out(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("refusing to store a naive datetime")
    return value.astimezone(UTC).isoformat()


def _dt_in(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _task_from_row(row: aiosqlite.Row) -> Task:
    spec_json = row["spec_json"]
    return Task(
        task_id=row["task_id"],
        directive=row["directive"],
        directive_hash=row["directive_hash"],
        status=TaskStatus(row["status"]),
        created_at=_dt_in(row["created_at"]),
        updated_at=_dt_in(row["updated_at"]),
        spec=TaskSpec.model_validate_json(spec_json) if spec_json else None,
        attempt_count=row["attempt_count"],
        cost_usd=Decimal(row["cost_usd"]),
        suspended_reason=row["suspended_reason"],
        resume_after=_dt_in(row["resume_after"]),
        note=row["note"],
        failure_class=(
            FailureClass(row["failure_class"]) if row["failure_class"] else None
        ),
    )


def _attempt_from_row(row: aiosqlite.Row) -> Attempt:
    return Attempt(
        schema_version=row["schema_version"],
        attempt_id=row["attempt_id"],
        task_id=row["task_id"],
        spec_id=row["spec_id"],
        spec_schema_version=row["spec_schema_version"],
        index=row["idx"],
        role=Role(row["role"]),
        model_id=row["model_id"],
        verdict=VerdictStatus(row["verdict"]),
        failure_class=FailureClass(row["failure_class"]),
        failure_reason=row["failure_reason"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        cost_usd=Decimal(row["cost_usd"]),
        latency_ms=row["latency_ms"],
        started_at=_dt_in(row["started_at"]),
        ended_at=_dt_in(row["ended_at"]),
    )


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


class StateStore:
    """Async SQLite store. Open with :meth:`connect`, close with :meth:`close`."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        clock: Clock | None = None,
        ids: IdSource | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock or SystemClock()
        self._ids = ids or UuidIds()

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    async def connect(
        cls,
        path: Path | str,
        clock: Clock | None = None,
        ids: IdSource | None = None,
    ) -> StateStore:
        path = Path(path)
        if path.parent != Path("") and str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        # WAL lets the UI process read while the daemon writes. Without it the
        # overlay would intermittently block the orchestrator on a lock.
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA synchronous = NORMAL")
        store = cls(conn, clock=clock, ids=ids)
        await store.migrate()
        return store

    async def close(self) -> None:
        await self._conn.close()

    async def __aenter__(self) -> StateStore:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # -- migrations --------------------------------------------------------

    async def schema_version(self) -> int:
        async with self._conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        return int(row[0])

    async def migrate(self) -> int:
        """Apply any migrations this database has not seen.

        Idempotent: running against an already-current database, empty or
        populated, is a no-op.
        """
        current = await self.schema_version()
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            await self._conn.executescript(sql)
            # user_version does not accept a bound parameter.
            await self._conn.execute(f"PRAGMA user_version = {int(version)}")
            await self._conn.commit()
            current = version
        return current

    # -- tasks -------------------------------------------------------------

    async def create_task(
        self,
        directive: str,
        *,
        task_id: str | None = None,
        status: TaskStatus = TaskStatus.PENDING,
        note: str | None = None,
    ) -> Task:
        """Create a task, hashing the directive at birth.

        The hash is what later checkpoints compare against, so it is taken from
        the raw text before anything has had a chance to reword it.
        """
        now = self._clock.now()
        task = Task(
            task_id=task_id or self._ids.new_id("task"),
            directive=directive,
            directive_hash=hash_directive(directive),
            status=status,
            created_at=now,
            updated_at=now,
            note=note,
        )
        await self._conn.execute(
            """
            INSERT INTO tasks (task_id, directive, directive_hash, status,
                               created_at, updated_at, spec_json, attempt_count,
                               cost_usd, suspended_reason, resume_after, note)
            VALUES (?, ?, ?, ?, ?, ?, NULL, 0, '0', NULL, NULL, ?)
            """,
            (
                task.task_id,
                task.directive,
                task.directive_hash,
                task.status.value,
                _dt_out(task.created_at),
                _dt_out(task.updated_at),
                task.note,
            ),
        )
        await self._conn.commit()
        return task

    async def get_task(self, task_id: str) -> Task:
        async with self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise TaskNotFound(task_id)
        return _task_from_row(row)

    async def find_task(self, task_id: str) -> Task | None:
        try:
            return await self.get_task(task_id)
        except TaskNotFound:
            return None

    async def list_tasks(
        self,
        *,
        status: TaskStatus | Iterable[TaskStatus] | None = None,
    ) -> list[Task]:
        """Tasks in creation order.

        Ordering is explicit and stable — the journal is rendered from this, and
        an unordered read would make it churn between identical states.
        """
        sql = "SELECT * FROM tasks"
        params: list[Any] = []
        if status is not None:
            statuses = [status] if isinstance(status, TaskStatus) else list(status)
            sql += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params.extend(s.value for s in statuses)
        sql += " ORDER BY created_at, task_id"
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_task_from_row(r) for r in rows]

    async def save_task(self, task: Task) -> Task:
        """Persist a task wholesale, stamping ``updated_at``."""
        task = task.model_copy(update={"updated_at": self._clock.now()})
        cur = await self._conn.execute(
            """
            UPDATE tasks
               SET directive = ?, directive_hash = ?, status = ?, updated_at = ?,
                   spec_json = ?, attempt_count = ?, cost_usd = ?,
                   suspended_reason = ?, resume_after = ?, note = ?,
                   failure_class = ?
             WHERE task_id = ?
            """,
            (
                task.directive,
                task.directive_hash,
                task.status.value,
                _dt_out(task.updated_at),
                task.spec.model_dump_json() if task.spec else None,
                task.attempt_count,
                str(task.cost_usd),
                task.suspended_reason,
                _dt_out(task.resume_after),
                task.note,
                task.failure_class.value if task.failure_class else None,
                task.task_id,
            ),
        )
        await self._conn.commit()
        if cur.rowcount == 0:
            raise TaskNotFound(task.task_id)
        return task

    async def delete_task(self, task_id: str) -> None:
        await self._conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        await self._conn.commit()

    async def upsert_task(self, task: Task) -> Task:
        """Write a task exactly as given, creating or replacing it.

        For recovery only. Unlike :meth:`create_task` it invents nothing — no id,
        no directive hash, no timestamps — because restoring from the journal
        must reproduce what was there, not mint a plausible replacement.
        """
        await self._conn.execute(
            """
            INSERT INTO tasks (task_id, directive, directive_hash, status,
                               created_at, updated_at, spec_json, attempt_count,
                               cost_usd, suspended_reason, resume_after, note,
                               failure_class)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                directive = excluded.directive,
                directive_hash = excluded.directive_hash,
                status = excluded.status,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                spec_json = excluded.spec_json,
                attempt_count = excluded.attempt_count,
                cost_usd = excluded.cost_usd,
                suspended_reason = excluded.suspended_reason,
                resume_after = excluded.resume_after,
                note = excluded.note,
                failure_class = excluded.failure_class
            """,
            (
                task.task_id,
                task.directive,
                task.directive_hash,
                task.status.value,
                _dt_out(task.created_at),
                _dt_out(task.updated_at),
                task.spec.model_dump_json() if task.spec else None,
                task.attempt_count,
                str(task.cost_usd),
                task.suspended_reason,
                _dt_out(task.resume_after),
                task.note,
                task.failure_class.value if task.failure_class else None,
            ),
        )
        await self._conn.commit()
        return task

    async def upsert_attempt(self, attempt: Attempt) -> Attempt:
        """Write an attempt row without touching the task's counters.

        For recovery only. :meth:`record_attempt` rolls ``attempt_count`` and
        ``cost_usd``; replaying rows through it would double-count, because the
        restored task already carries those totals.
        """
        await self._conn.execute(
            """
            INSERT INTO attempts (attempt_id, task_id, spec_id, schema_version,
                                  spec_schema_version, idx, role, model_id,
                                  verdict, failure_class, failure_reason,
                                  tokens_in, tokens_out, cost_usd, latency_ms,
                                  started_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(attempt_id) DO NOTHING
            """,
            (
                attempt.attempt_id,
                attempt.task_id,
                attempt.spec_id,
                attempt.schema_version,
                attempt.spec_schema_version,
                attempt.index,
                attempt.role.value,
                attempt.model_id,
                attempt.verdict.value,
                attempt.failure_class.value,
                attempt.failure_reason,
                attempt.tokens_in,
                attempt.tokens_out,
                str(attempt.cost_usd),
                attempt.latency_ms,
                _dt_out(attempt.started_at),
                _dt_out(attempt.ended_at),
            ),
        )
        await self._conn.commit()
        return attempt

    # -- attempts ----------------------------------------------------------

    async def record_attempt(self, attempt: Attempt) -> Attempt:
        """Append one attempt row and roll the task's counters.

        One row per attempt, never per task: a task that fails at ``low`` and
        passes at ``high`` yields two labelled rows, which is how the router gets
        evidence about tiers it did not ultimately use.
        """
        await self._conn.execute(
            """
            INSERT INTO attempts (attempt_id, task_id, spec_id, schema_version,
                                  spec_schema_version, idx, role, model_id,
                                  verdict, failure_class, failure_reason,
                                  tokens_in, tokens_out, cost_usd, latency_ms,
                                  started_at, ended_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.attempt_id,
                attempt.task_id,
                attempt.spec_id,
                attempt.schema_version,
                attempt.spec_schema_version,
                attempt.index,
                attempt.role.value,
                attempt.model_id,
                attempt.verdict.value,
                attempt.failure_class.value,
                attempt.failure_reason,
                attempt.tokens_in,
                attempt.tokens_out,
                str(attempt.cost_usd),
                attempt.latency_ms,
                _dt_out(attempt.started_at),
                _dt_out(attempt.ended_at),
            ),
        )
        await self._conn.execute(
            """
            UPDATE tasks
               SET attempt_count = attempt_count + 1,
                   updated_at = ?
             WHERE task_id = ?
            """,
            (_dt_out(self._clock.now()), attempt.task_id),
        )
        await self._conn.commit()

        # The cost rollup belongs to record_spend, not here. Doing it in both
        # places is how a task's total ends up counting execution twice and
        # planning not at all.
        await self.record_spend(
            purpose="attempt",
            role=attempt.role,
            model_id=attempt.model_id,
            cost_usd=attempt.cost_usd,
            tokens_in=attempt.tokens_in,
            tokens_out=attempt.tokens_out,
            task_id=attempt.task_id,
            spend_id=f"spend_{attempt.attempt_id}",
            at=attempt.started_at,
            billable=attempt.billable,
        )
        return attempt

    async def list_attempts(self, task_id: str) -> list[Attempt]:
        async with self._conn.execute(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY idx", (task_id,)
        ) as cur:
            rows = await cur.fetchall()
        return [_attempt_from_row(r) for r in rows]

    async def next_attempt_index(self, task_id: str) -> int:
        async with self._conn.execute(
            "SELECT COALESCE(MAX(idx), -1) + 1 AS nxt FROM attempts WHERE task_id = ?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row["nxt"])

    # -- spend -------------------------------------------------------------

    async def record_spend(
        self,
        *,
        purpose: str,
        role: Role,
        model_id: str,
        cost_usd: Decimal,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cached_in: int = 0,
        task_id: str | None = None,
        spend_id: str | None = None,
        at: datetime | None = None,
        billable: bool = True,
    ) -> Decimal:
        """Record one billable model call.

        Every call goes here — planning, test authorship, execution — so the
        budget guard sees the whole bill rather than only the part that happened
        to be an attempt.
        """
        await self._conn.execute(
            """
            INSERT INTO spend (spend_id, task_id, purpose, role, model_id,
                               tokens_in, tokens_out, cached_in, cost_usd, at,
                               billable)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                spend_id or self._ids.new_id("spend"),
                task_id,
                purpose,
                role.value,
                model_id,
                tokens_in,
                tokens_out,
                cached_in,
                str(cost_usd),
                _dt_out(at or self._clock.now()),
                1 if billable else 0,
            ),
        )

        # Roll the task's running total here, in the one place that sees every
        # billable call. Summed in Python, not SQL: SQLite has no decimal type,
        # so doing it in the query would round-trip through a float and quietly
        # undo the exactness the TEXT column exists to preserve.
        if task_id is not None:
            async with self._conn.execute(
                "SELECT cost_usd FROM tasks WHERE task_id = ?", (task_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise TaskNotFound(task_id)
            await self._conn.execute(
                "UPDATE tasks SET cost_usd = ?, updated_at = ? WHERE task_id = ?",
                (
                    str(Decimal(row["cost_usd"]) + cost_usd),
                    _dt_out(self._clock.now()),
                    task_id,
                ),
            )

        await self._conn.commit()
        return cost_usd

    async def spend_on(self, day: date) -> Decimal:
        """Total spend for a UTC day, summed exactly from the spend ledger.

        Recomputed rather than kept as a running total: a counter that drifts
        from the ledger is worse than no counter, because the budget guard would
        then enforce a fiction.
        """
        async with self._conn.execute(
            "SELECT cost_usd FROM spend "
            "WHERE substr(at, 1, 10) = ? AND billable = 1",
            (day.isoformat(),),
        ) as cur:
            rows = await cur.fetchall()
        return sum((Decimal(r["cost_usd"]) for r in rows), Decimal("0"))

    async def task_spend(self, task_id: str, *, billable_only: bool = True) -> Decimal:
        """What this task has cost, including the conductor.

        Reads the spend ledger rather than the attempt rows. Summing attempts
        counted only execution and left planning and test authorship outside the
        ceiling entirely.

        ``billable_only`` is the default because the budget guard is the main
        caller and a ceiling must only stop real money. Pass False for reporting,
        where the flat-rate plane's list-equivalent price is the interesting
        number — it is what a run *would* have cost without the subscription.
        """
        sql = "SELECT cost_usd FROM spend WHERE task_id = ?"
        if billable_only:
            sql += " AND billable = 1"
        async with self._conn.execute(sql, (task_id,)) as cur:
            rows = await cur.fetchall()
        return sum((Decimal(r["cost_usd"]) for r in rows), Decimal("0"))

    async def spend_breakdown(self, task_id: str) -> dict[str, Decimal]:
        """Cost per purpose — answers "where did the money actually go"."""
        async with self._conn.execute(
            "SELECT purpose, cost_usd FROM spend WHERE task_id = ?", (task_id,)
        ) as cur:
            rows = await cur.fetchall()
        out: dict[str, Decimal] = {}
        for row in rows:
            out[row["purpose"]] = out.get(row["purpose"], Decimal("0")) + Decimal(row["cost_usd"])
        return out
