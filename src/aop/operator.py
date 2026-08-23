"""The composition root.

Every plane built in Slots 01–40 exists in isolation and is tested in isolation.
This is the one place they are wired together, and the one place that knows the
order things happen in:

    directive → checkpoint → spec → rationale → route → author tests
              → ladder → verdict → journal

Nothing here should contain new logic. If a rule appears in this file that is not
merely *sequencing*, it belongs in the plane it came from — the whole point of the
slot structure is that each rule has exactly one home. The pipeline below is
deliberately readable top to bottom for that reason: it should be possible to
check it against the spec by eye.

The conductor is woken at defined checkpoints and nowhere else. Escalation happens
inside the ladder without consulting it, because that is the single largest cost
decision in the system.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

from aop.backends import build_backend
from aop.conductor import (
    CheckpointLog,
    DirectiveGuard,
    SpecEmissionFailed,
    author_acceptance_tests,
    record_rationale,
)
from aop.conductor.taskspec import emit_spec
from aop.context import ContextAssembler
from aop.core.config import Settings
from aop.core.events import EventBus, LogLine
from aop.core.failures import Action
from aop.core.ids import Clock, IdSource, SystemClock, UuidIds
from aop.core.journal import Journal
from aop.core.lifecycle import TaskLifecycle
from aop.core.scheduler import Scheduler
from aop.core.schemas import FailureClass, Role, Strict, Task, TaskSpec, TaskStatus
from aop.core.state import StateStore
from aop.execution import (
    EscalationLadder,
    ExecutionPlane,
    ProviderRoutedPlane,
    Worker,
    build_toolbox,
)
from aop.guards import BudgetGuard, DiscoveryScope, PathJail
from aop.memory.logbook import Logbook
from aop.registry import Registry
from aop.registry.adapter import Adapter, AdapterError, Message
from aop.registry.cost import CostModel
from aop.registry.providers import MockProvider
from aop.router import RuleRouter
from aop.verify import PytestGate, VerifierRegistry, VerifyContext

CONDUCTOR_INSTRUCTIONS = """\
You are the conductor of an autonomous operator. You plan and delegate; you do \
not implement.

Return a single JSON object describing the task, matching the schema you were \
given. Fill the fields; do not restate the directive in your own words — the \
directive is immutable and is judged as written.

`acceptance` is REQUIRED and must not be empty. Each entry is one concrete, \
checkable statement about observable behaviour — the kind of thing a test can \
assert. These become the acceptance tests, written by someone other than the \
implementer and frozen before implementation starts, so vague criteria produce a \
gate that passes without proving anything.

Good:  "exponential_backoff(0) returns 1"  ·  "the delay never exceeds 60"
Bad:   "the code works"  ·  "it is well written"  ·  "handles edge cases"

A plan with no acceptance criteria is rejected before any worker sees it.\
"""

WORKER_INSTRUCTIONS = """\
You are an execution worker. Do the task described, using the tools provided.

Everything you produce passes a deterministic verifier before it is trusted, so \
claiming success does not make something true. If a tool denies an action, read \
the reason and adjust — it is a guard, not a suggestion, and arguing with it \
wastes a turn.\
"""


class RunOutcome(Strict):
    """What happened to one directive, end to end."""

    task_id: str
    action: Action
    spec: TaskSpec | None = None
    role: Role | None = None
    attempts: int = 0
    cost_usd: Decimal = Decimal("0")
    verdict_reason: str | None = None
    note: str = ""


class Operator:
    """Owns the running system: state, bus, models, guards, and the pipeline."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport=None,
        clock: Clock | None = None,
        ids: IdSource | None = None,
        gate: VerifierRegistry | None = None,
        plane: ExecutionPlane | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock or SystemClock()
        self.ids = ids or UuidIds()

        self.bus = EventBus(clock=self.clock, ids=self.ids)
        self.registry = Registry(settings.registry)
        self.cost = CostModel(self.registry)

        self.jail = PathJail(settings.jail_root)
        self.jail.root.mkdir(parents=True, exist_ok=True)
        self.backend = build_backend(settings.policy, self.jail)
        # Where a worker may *look*, as opposed to where it may write. A second,
        # differently shaped guard: an allowlist of roots rather than one root,
        # and read-only. Empty by default, so it grants nothing until asked.
        self.discovery = DiscoveryScope(
            settings.policy.discovery.roots,
            deny_dirs=settings.policy.discovery.deny_dirs,
            deny_names=settings.policy.discovery.deny_names,
            max_results=settings.policy.discovery.max_results,
        )

        self.mock: MockProvider | None = None
        mounts = None if transport is not None else self._mock_mounts()
        self.adapter = Adapter(
            self.registry, cost_model=self.cost, bus=self.bus,
            clock=self.clock, transport=transport, mounts=mounts,
            max_retries=settings.policy.execution.max_transport_retries,
            retry_backoff_seconds=settings.policy.execution.retry_backoff_seconds,
        )
        self.worker = Worker(
            self.adapter, self.registry, bus=self.bus, clock=self.clock,
            max_tool_iterations=settings.policy.execution.max_tool_iterations,
        )
        # The plane that runs *attempts*. Authorship deliberately keeps the
        # internal worker whichever plane is selected: the test author and the
        # implementer must not be the same actor, and putting them on different
        # planes entirely is a stronger separation than the frozen file alone.
        self.plane: ExecutionPlane = plane or self._build_plane()
        # Injected or selected, the name is recorded rather than inferred. A
        # report that names the plane from config while something else actually
        # ran is the same wrong answer `_build_plane` refuses to give.
        self.plane_name: str = (
            type(plane).__name__ if plane is not None
            else settings.policy.execution.plane
        )
        self.router = RuleRouter(self.registry)
        self.checkpoints = CheckpointLog(settings.policy.effort, self.bus)

        # Default gate: run the workspace's own suite. Injectable so a demo or a
        # test can supply a different one without the pipeline knowing.
        self._gate = gate or self._default_gate()

        # Populated by start().
        self.store: StateStore | None = None
        self.lifecycle: TaskLifecycle | None = None
        self.logbook: Logbook | None = None
        self.journal: Journal | None = None
        self.budget: BudgetGuard | None = None
        self.scheduler: Scheduler | None = None

    def _mock_mounts(self) -> dict | None:
        """Honour ``provider = "mock"`` outside the test suite.

        Without this the registry says *mock* while the client tries to resolve
        the mock's host for real — the config and the behaviour disagree, and the
        daemon fails on its first call. Every service test injected a transport,
        so none of them noticed; only running it did.

        Mounted by host rather than replacing the transport, so a registry that
        mixes the mock with a real provider keeps working during the swap.
        """
        hosts = {
            entry.base_url.rsplit("/", 1)[0] if entry.base_url.count("/") > 2 else entry.base_url
            for entry in self.settings.registry.roles.values()
            if entry.provider == "mock"
        }
        if not hosts:
            return None

        self.mock = MockProvider(plausible=True)
        transport = self.mock.transport()
        mounts: dict = {}
        for base in hosts:
            scheme, _, rest = base.partition("://")
            mounts[f"{scheme}://{rest.split('/')[0]}"] = transport
        return mounts

    def _build_plane(self) -> ExecutionPlane:
        """Assemble the plane that dispatches attempts.

        Sequencing, not logic: each plane owns its own behaviour, and which one
        a role uses is decided by that role's *provider* — read per dispatch, so
        it follows a Slot 48c failover instead of going stale the moment one
        happens. See :class:`ProviderRoutedPlane`.

        Local planes are built **here**, eagerly, even though routing is lazy.
        Both of `ClaudeCodePlane`'s startup checks — the SDK import and the
        `claude` binary on PATH — exist because discovering either one mid-run
        already cost a real measurement. Fail at construction, not ninety minutes
        into a paid run.
        """
        declared = self.settings.policy.execution.plane
        if declared not in ("internal", "claude_code"):
            raise NotImplementedError(f"no such execution plane: {declared!r}")

        local: dict[str, ExecutionPlane] = {}
        if declared == "claude_code" or self._uses_provider("claude_code"):
            from aop.execution.claude_code import ClaudeCodePlane

            local["claude_code"] = ClaudeCodePlane(
                self.registry,
                self.jail,
                max_turns=self.settings.policy.execution.claude_max_turns,
                bus=self.bus,
                clock=self.clock,
            )
        return ProviderRoutedPlane(self.registry, default=self.worker, local=local)

    def _uses_provider(self, provider: str) -> bool:
        """Whether any role could reach this provider — fallbacks included.

        The chain matters as much as the primary: a role whose primary is an
        HTTP vendor and whose fallback is `claude_code` needs that plane built
        before the failover happens, not after it has already failed.
        """
        for entry in self.settings.registry.roles.values():
            if entry.provider == provider:
                return True
            if any(alt.provider == provider for alt in entry.fallback):
                return True
        return False

    def _default_gate(self) -> VerifierRegistry:
        gate = VerifierRegistry()
        gate.register(
            PytestGate(self.backend, timeout=self.settings.policy.execution.command_timeout_seconds)
        )
        return gate

    # -- lifecycle ---------------------------------------------------------

    @property
    def db_path(self) -> Path:
        """Durable state lives *outside* the jail, deliberately.

        Inside it, a worker could read the attempt log or edit the task's own
        status — marking itself complete is a far cheaper way to pass than doing
        the work. The journal is written into the jail because it is meant to be
        readable; the database is not.
        """
        return self.settings.project_root / "state.db"

    async def start(self, *, run_scheduler: bool = True) -> None:
        """Open durable state, reclaim anything a crash left behind, write the journal.

        ``run_scheduler=False`` leaves the loop stopped, for callers that want to
        drive :meth:`run` themselves. With the loop live, the scheduler owns
        execution and driving the pipeline by hand races it for the same task.
        """
        self.store = await StateStore.connect(self.db_path, clock=self.clock, ids=self.ids)
        self.lifecycle = TaskLifecycle(self.store, self.bus, clock=self.clock)
        self.logbook = Logbook(self.store, bus=self.bus, clock=self.clock, ids=self.ids)
        self.journal = Journal(
            self.store, self.jail.root / "OPERATOR.md", bus=self.bus, clock=self.clock
        )
        # The journal lives in the jail so it is readable from the workspace, but
        # it is what recovery parses — a worker able to edit it could mark itself
        # complete, which is a much cheaper way to pass than doing the work.
        # Freezing gives exactly the right semantics: context it can read, not a
        # record it can rewrite.
        self.jail.freeze(self.journal.path)
        self.budget = BudgetGuard(
            self.settings.policy.budget, self.store, bus=self.bus, clock=self.clock
        )

        self.scheduler = Scheduler(
            self._guarded_run, self.lifecycle, self.settings.policy.scheduler,
            bus=self.bus, clock=self.clock,
        )

        # Reclaim first, then start the loop — so anything a crash interrupted is
        # already PENDING when the scheduler takes its first look, and gets
        # resumed rather than stranded.
        recovered = await self.lifecycle.recover_orphans()
        if recovered:
            self.bus.emit(
                LogLine, level="warn",
                message=f"reclaimed {len(recovered)} task(s) orphaned by a restart",
                detail={"resume": str(self.settings.policy.scheduler.resume_on_start)},
            )
        await self.journal.write()
        if run_scheduler:
            await self.scheduler.start()

    async def stop(self, *, drain: bool = False) -> None:
        if self.scheduler is not None:
            await self.scheduler.stop(drain=drain)
        await self.adapter.aclose()
        if self.store is not None:
            await self.store.close()
        self.bus.close()

    async def __aenter__(self) -> Operator:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # -- submission --------------------------------------------------------

    async def submit(self, directive: str) -> Task:
        """Accept a directive and queue it.

        Returns as soon as the task exists, not when it finishes — a directive
        that blocked until done would make the UI feel dead for the whole run,
        and the event stream is how progress is reported.

        Launching goes through the scheduler rather than spawning here, so there
        is exactly one place that knows what is running. Two registries would
        drift, and the symptom would be a task run twice.
        """
        task = await self.lifecycle.create(directive)
        if not self.scheduler.launch(task.task_id):
            # At capacity. It stays PENDING and the next tick collects it, which
            # is why a queue is better than a spawn: nothing is dropped.
            self.bus.emit(
                LogLine, task_id=task.task_id, level="info",
                message="queued — the scheduler is at capacity",
                detail={"in_flight": str(len(self.scheduler.in_flight))},
            )
        return task

    async def run_directive(self, directive: str, *, timeout: float | None = None) -> Task:
        """Submit one directive and wait for it to settle.

        The supported one-shot path. It goes through the scheduler like anything
        else, because a second way to start work is a second thing that can race
        the first.
        """
        task = await self.submit(directive)
        while not await self.scheduler.wait_for(task.task_id, timeout=timeout):
            # Queued behind something else — let a tick collect it.
            await self.scheduler.tick()
            if not self.scheduler.is_running(task.task_id):
                await asyncio.sleep(self.settings.policy.scheduler.tick_seconds)
            current = await self.store.get_task(task.task_id)
            if current.status.terminal or current.status is TaskStatus.AWAITING_HUMAN:
                return current
        return await self.store.get_task(task.task_id)

    async def _guarded_run(self, task_id: str) -> RunOutcome:
        """Never let a pipeline crash take the daemon with it.

        The class of the failure is recorded, not just the fact of it. An
        ``AdapterError`` means the vendor or the wire gave out *before* the model
        was ever judged — that is a broken tool, not a weak one, and anything
        downstream that reads task status alone would otherwise score an outage
        as a capability result.
        """
        try:
            return await self.run(task_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a wedged daemon is worse
            failure_class = (
                FailureClass.TRANSPORT if isinstance(exc, AdapterError) else None
            )
            self.bus.emit(
                LogLine, task_id=task_id, level="error",
                message="the pipeline failed",
                detail={
                    "error": f"{type(exc).__name__}: {exc}",
                    "failure_class": failure_class.value if failure_class else "unclassified",
                },
            )
            await self.lifecycle.fail(
                task_id, f"{type(exc).__name__}: {exc}", failure_class=failure_class
            )
            await self.journal.write()
            return RunOutcome(task_id=task_id, action=Action.HAND_TO_HUMAN, note=str(exc))

    # -- the pipeline ------------------------------------------------------

    async def run(self, task_id: str) -> RunOutcome:
        task = await self.store.get_task(task_id)
        guard = DirectiveGuard(task.directive)
        guard.verify_task(task)

        # A resumed task is already RUNNING — the scheduler moved it there when it
        # woke. Starting it again would be an illegal transition, and the pipeline
        # would kill the very task it was asked to continue.
        #
        # Note what "continue" means today: there is no continuation record, so a
        # resumed task re-runs the pipeline from the top. That is correct but not
        # free, and it is the reason mid-ladder suspension is still not wired in —
        # suspending inside a climb would lose the climb.
        if task.status is not TaskStatus.RUNNING:
            await self.lifecycle.start(task_id)

        # 1 — checkpoint: plan. One of exactly four events that wake the conductor.
        checkpoint = self.checkpoints.enter("task_created", task_id=task_id)

        planning = ContextAssembler(
            task.directive, CONDUCTOR_INSTRUCTIONS, bus=self.bus, task_id=task_id
        )
        try:
            emission = await emit_spec(
                self.adapter,
                planning.messages() + [Message.user("Produce the task spec.")],
                task_id=task_id,
                ids=self.ids,
                policy=self.settings.policy.conductor,
                effort=checkpoint.effort,
            )
        except SpecEmissionFailed as exc:
            await self.lifecycle.await_human(task_id, str(exc))
            await self.journal.write()
            return RunOutcome(task_id=task_id, action=Action.HAND_TO_HUMAN, note=str(exc))

        spec = emission.spec
        await self.store.save_task(
            (await self.store.get_task(task_id)).model_copy(update={"spec": spec})
        )

        # Planning is billable and was previously invisible to the budget guard,
        # which is the wrong way round — the conductor is the component the spec
        # names as cost risk #1.
        await self.store.record_spend(
            purpose="plan", role=Role.CONDUCTOR,
            model_id=self.registry.model_id(Role.CONDUCTOR),
            cost_usd=emission.cost_usd,
            tokens_in=emission.usage.tokens_in,
            tokens_out=emission.usage.tokens_out,
            cached_in=emission.usage.cached_in,
            task_id=task_id,
        )

        # 2 — the plan is checked deterministically. The conductor's own account
        # of why its plan is faithful is recorded and carries no weight.
        rationale = record_rationale(
            task_id, task.directive, spec, stated=emission.raw[:2000],
            require_falsifiable=(
                self.settings.policy.conductor.require_falsifiable_directive
            ),
        )
        self.bus.emit(
            LogLine, task_id=task_id,
            level="info" if rationale.check.ok else "error",
            message="plan checked against directive",
            detail={
                "ok": str(rationale.check.ok),
                "overlap": f"{rationale.check.overlap:.0%}",
                "problems": "; ".join(rationale.check.problems) or "none",
            },
        )
        if not rationale.trustworthy:
            reason = "; ".join(rationale.check.problems)
            await self.lifecycle.await_human(task_id, f"plan rejected: {reason}")
            await self.journal.write()
            return RunOutcome(task_id=task_id, action=Action.HAND_TO_HUMAN, spec=spec, note=reason)

        # 3 — routing. Difficulty picks a tier; modality can override it.
        decision = self.router.route(spec)
        self.bus.emit(
            LogLine, task_id=task_id, level="info", message="routed",
            detail={
                "role": decision.role.value,
                "desired": decision.desired.value,
                "score": str(decision.score),
                "why": decision.rationale,
            },
        )

        # 4 — acceptance tests are authored first and frozen, so the implementer
        # is graded by something it cannot edit.
        authored = await author_acceptance_tests(
            self.worker, spec, self.jail,
            policy=self.settings.policy.conductor, task_id=task_id,
        )
        if authored.author_role is not None:
            await self.store.record_spend(
                purpose="authorship", role=authored.author_role,
                model_id=self.registry.model_id(authored.author_role),
                cost_usd=Decimal(authored.cost_usd),
                task_id=task_id,
            )
        if authored.frozen:
            self.bus.emit(
                LogLine, task_id=task_id, level="info",
                message="acceptance tests authored and frozen",
                detail={"path": authored.path, "author": (authored.author_role or Role.LOW).value},
            )

        # 5 — the ladder. Escalation happens in here without waking the conductor.
        working = ContextAssembler(
            task.directive, WORKER_INSTRUCTIONS, bus=self.bus, task_id=task_id
        )
        ladder = EscalationLadder(
            self.plane, self._gate, self.registry, self.logbook,
            self.settings.policy.ladder, budget=self.budget, bus=self.bus, clock=self.clock,
        )
        result = await ladder.run(
            task_id, spec, working,
            build_toolbox(self.jail, self.backend, discovery=self.discovery),
            VerifyContext(
                task_id=task_id, spec=spec, workspace=self.jail.root,
                target=authored.path if authored.written else None,
            ),
            start_role=decision.role,
            effort=None,
            features=decision.features,
        )

        # 6 — settle the task, and re-check the directive one last time. Recovery
        # must reproduce the hash, never recompute it.
        guard.verify_task(await self.store.get_task(task_id))

        if result.final_action is Action.PROCEED:
            self.checkpoints.enter("work_verified", task_id=task_id)
            await self.lifecycle.complete(task_id, note=f"verified by {result.verdict.verifier}")
        elif result.final_action is Action.HALT:
            await self.lifecycle.halt(task_id, result.verdict.reason or "budget ceiling")
        else:
            self.checkpoints.enter("ladder_exhausted", task_id=task_id)
            await self.lifecycle.await_human(
                task_id, result.verdict.reason or "the ladder was exhausted"
            )

        await self.journal.write()
        return RunOutcome(
            task_id=task_id,
            action=result.final_action,
            spec=spec,
            role=decision.role,
            attempts=result.attempts,
            # The whole bill, not just the ladder: planning and test authorship
            # are real spend and belong in the number the caller is shown.
            cost_usd=await self.store.task_spend(task_id),
            verdict_reason=result.verdict.reason,
        )

    # -- reads for the UI --------------------------------------------------

    async def snapshot(self) -> dict:
        """Everything the overlay needs to render itself on connect."""
        tasks = await self.store.list_tasks()
        today = self.clock.now().date()
        return {
            "tasks": [t.model_dump(mode="json") for t in tasks],
            "spend_today": str(await self.store.spend_on(today)),
            "budget_day": str(self.settings.policy.budget.per_day_usd),
            "roles": {
                role.value: {
                    "model": self.registry.model_id(role),
                    "provider": self.registry.provider(role),
                    "free": self.registry.is_free(role),
                }
                for role in Role
            },
            "checkpoints": self.checkpoints.count,
            "in_flight": sorted(self.scheduler.in_flight) if self.scheduler else [],
            "capacity": self.scheduler.capacity if self.scheduler else 0,
            "jail_root": str(self.jail.root),
            "backend": self.settings.policy.execution.backend,
        }
