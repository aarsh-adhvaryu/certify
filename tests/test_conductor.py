"""Slots 33–40 — the conductor and the router.

The tests that carry these blocks: no conductor call outside a checkpoint, an
invalid spec never reaches a worker, and the implementer cannot edit the tests it
is graded by.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aop.conductor import (
    WAKES_CONDUCTOR,
    Checkpoint,
    CheckpointLog,
    DirectiveGuard,
    DirectiveViolation,
    NotACheckpoint,
    SpecEmissionFailed,
    author_acceptance_tests,
    check_plan,
    checkpoint_for,
    default_test_path,
    effort_for,
    emit_spec,
    extract_json,
    freeze_existing,
    parse_spec,
    record_rationale,
    spec_schema,
)
from aop.context import ContextAssembler
from aop.core.config import ConductorPolicy, EffortPolicy, Authorship, load_settings
from aop.core.ids import FrozenClock, SequentialIds
from aop.core.schemas import (
    Difficulty,
    ReasoningEffort,
    Role,
    Task,
    TaskSpec,
    TaskStatus,
    hash_directive,
)
from aop.execution import Worker
from aop.guards import GuardDenied, PathJail
from aop.registry import Registry
from aop.registry.adapter import Adapter, Message
from aop.registry.providers import MockProvider, MockReply
from aop.router import FEATURE_NAMES, RuleRouter, extract, to_vector

PROJECT_CONFIG = Path(__file__).resolve().parent / "config"
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
DIRECTIVE = "add exponential backoff to the S3 uploader"


@pytest.fixture
def registry() -> Registry:
    return Registry(load_settings(PROJECT_CONFIG).registry, env={})


@pytest.fixture
def provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
async def adapter(registry, provider):
    a = Adapter(registry, transport=provider.transport(), clock=FrozenClock(T0, step=timedelta(0)))
    yield a
    await a.aclose()


@pytest.fixture
def jail(tmp_path) -> PathJail:
    root = tmp_path / "workspace"
    root.mkdir()
    return PathJail(root)


def _spec(**over) -> TaskSpec:
    base = dict(
        spec_id="spec_0001",
        task_id="task_0001",
        goal="add exponential backoff to the uploader",
        acceptance=["retries exactly three times"],
    )
    return TaskSpec(**{**base, **over})


# ============================================= Slot 33 — the directive


def test_directive_is_hashed_from_exact_bytes():
    guard = DirectiveGuard(DIRECTIVE)
    guard.verify(DIRECTIVE)

    for reworded in [DIRECTIVE.upper(), DIRECTIVE + " ", DIRECTIVE.replace("S3", "s3")]:
        with pytest.raises(DirectiveViolation):
            guard.verify(reworded)


def test_a_restated_directive_is_refused():
    """The failure mode: not malice, just drift — the third restatement is
    describing a different task."""
    guard = DirectiveGuard(DIRECTIVE)
    with pytest.raises(DirectiveViolation, match="immutable"):
        guard.verify("make the uploader more reliable")


def test_task_row_is_checked_on_both_fields():
    """A corrupted row could have a directive and hash that agree with each
    other while disagreeing with what was actually asked."""
    guard = DirectiveGuard(DIRECTIVE)
    good = Task(
        task_id="task_0001", directive=DIRECTIVE, directive_hash=hash_directive(DIRECTIVE),
        status=TaskStatus.PENDING, created_at=T0, updated_at=T0,
    )
    guard.verify_task(good)

    tampered = good.model_copy(update={"directive_hash": "0" * 64})
    with pytest.raises(DirectiveViolation, match="hash"):
        guard.verify_task(tampered)


def test_directive_must_stay_verbatim_in_the_prefix():
    guard = DirectiveGuard(DIRECTIVE)
    assembler = ContextAssembler(DIRECTIVE, "instructions")
    guard.verify_context(assembler)

    other = ContextAssembler("a different directive entirely", "instructions")
    with pytest.raises(DirectiveViolation):
        guard.verify_context(other)


def test_checks_are_counted():
    guard = DirectiveGuard(DIRECTIVE)
    for _ in range(3):
        guard.verify(DIRECTIVE)
    assert guard.checks == 3


# ========================================== Slot 34 — spec emission


def test_schema_excludes_fields_the_orchestrator_assigns():
    """Ids that come from a model are ids that collide."""
    props = spec_schema()["properties"]
    assert "spec_id" not in props and "task_id" not in props
    assert "goal" in props and "acceptance" in props


@pytest.mark.parametrize(
    "wrapped",
    [
        '{"goal": "x"}',
        '```json\n{"goal": "x"}\n```',
        'Here is the spec:\n```\n{"goal": "x"}\n```\nHope that helps.',
        'Sure! {"goal": "x"}',
    ],
)
def test_json_is_extracted_from_however_the_model_wrapped_it(wrapped):
    """Rejecting a structurally correct spec over a code fence would burn a
    repair round on formatting rather than substance."""
    assert json.loads(extract_json(wrapped))["goal"] == "x"


def test_parse_assigns_ids_and_ignores_any_the_model_supplied():
    spec = parse_spec(
        '{"goal": "x", "spec_id": "model-invented", "task_id": "wrong"}',
        spec_id="spec_0001", task_id="task_0001",
    )
    assert spec.spec_id == "spec_0001"
    assert spec.task_id == "task_0001"


async def test_a_valid_spec_is_accepted_first_time(adapter, provider):
    provider.script("mock-conductor", MockReply(content='{"goal": "add backoff", "acceptance": ["retries 3x"]}'))
    result = await emit_spec(adapter, [Message.user("plan it")], task_id="task_0001", ids=SequentialIds())

    assert result.spec.goal == "add backoff"
    assert result.repairs == 0


async def test_an_invalid_spec_is_repaired_not_passed_downstream(adapter, provider):
    """Accepting a half-valid spec and letting the worker interpret the gaps
    would put the drift back in through the side door."""
    provider.script(
        "mock-conductor",
        MockReply(content='{"goal": "x", "difficulty_hint": "impossible"}'),
        MockReply(content='{"goal": "x", "difficulty_hint": "hard"}'),
    )
    result = await emit_spec(adapter, [Message.user("plan")], task_id="task_0001", ids=SequentialIds())

    assert result.repairs == 1
    assert result.spec.difficulty_hint is Difficulty.HARD


async def test_the_repair_message_names_the_field(adapter, provider):
    provider.script(
        "mock-conductor",
        MockReply(content='{"goal": "x", "needs_pixels": "yes please"}'),
        MockReply(content='{"goal": "x"}'),
    )
    await emit_spec(adapter, [Message.user("plan")], task_id="task_0001", ids=SequentialIds())

    repair = provider.calls[1]["messages"][-1]["content"]
    assert "needs_pixels" in repair


async def test_giving_up_raises_rather_than_guessing(adapter, provider):
    provider.script("mock-conductor", MockReply(content="not json at all"))
    with pytest.raises(SpecEmissionFailed, match="no valid task spec"):
        await emit_spec(
            adapter, [Message.user("plan")], task_id="task_0001",
            ids=SequentialIds(), policy=ConductorPolicy(max_spec_repair_attempts=1),
        )


async def test_repairs_append_and_keep_the_conversation(adapter, provider):
    """A repair round is cheap for the same reason a retry is: nothing is rebuilt."""
    provider.script(
        "mock-conductor",
        MockReply(content="garbage"),
        MockReply(content='{"goal": "x"}'),
    )
    await emit_spec(adapter, [Message.user("plan it")], task_id="task_0001", ids=SequentialIds())

    assert len(provider.calls[1]["messages"]) > len(provider.calls[0]["messages"])
    assert provider.calls[1]["messages"][0] == provider.calls[0]["messages"][0]


# ================================ Slots 35, 36 — checkpoints and effort


def test_the_four_checkpoints():
    assert checkpoint_for("task_created") is Checkpoint.PLAN
    assert checkpoint_for("ladder_exhausted") is Checkpoint.REPLAN
    assert checkpoint_for("worker_question") is Checkpoint.WORKER_QUESTION
    assert checkpoint_for("work_verified") is Checkpoint.REVIEW


@pytest.mark.parametrize(
    "event",
    ["verifier_passed", "verifier_failed", "retry_started", "tier_escalated",
     "token_streamed", "guard_denied", "tool_called"],
)
def test_ordinary_events_do_not_wake_the_conductor(event):
    """Waking on every event is the single biggest way to inflate the bill, so
    an event that quietly became a conductor call must fail loudly."""
    with pytest.raises(NotACheckpoint, match="inflate the bill"):
        checkpoint_for(event)


def test_escalation_is_explicitly_not_a_checkpoint():
    """Verifier-driven by design; re-planning the path already going badly is
    the most expensive available reflex."""
    assert WAKES_CONDUCTOR["tier_escalated"] is False


def test_an_unknown_event_does_not_wake_the_conductor():
    with pytest.raises(NotACheckpoint):
        checkpoint_for("something_new_someone_added")


def test_effort_is_low_for_routine_coordination():
    policy = EffortPolicy()
    assert effort_for(Checkpoint.PLAN, policy) is ReasoningEffort.LOW
    assert effort_for(Checkpoint.REVIEW, policy) is ReasoningEffort.LOW
    assert effort_for(Checkpoint.WORKER_QUESTION, policy) is ReasoningEffort.LOW


def test_replanning_gets_more_thinking():
    assert effort_for(Checkpoint.REPLAN, EffortPolicy()) is ReasoningEffort.HIGH


def test_effort_comes_from_policy_not_code():
    frugal = EffortPolicy(default=ReasoningEffort.LOW, on_replan=ReasoningEffort.LOW)
    assert effort_for(Checkpoint.REPLAN, frugal) is ReasoningEffort.LOW


def test_checkpoints_are_logged_with_their_effort():
    """'Why did this task cost so much' is answered by counting checkpoints."""
    log = CheckpointLog(EffortPolicy())
    log.enter("task_created")
    log.enter("ladder_exhausted", reason="low and high both failed")

    assert log.count == 2
    assert log.effort_histogram() == {ReasoningEffort.LOW: 1, ReasoningEffort.HIGH: 1}


def test_logging_a_non_checkpoint_is_refused():
    with pytest.raises(NotACheckpoint):
        CheckpointLog(EffortPolicy()).enter("verifier_failed")


# ============================== Slot 37 — test-authorship separation


@pytest.fixture
async def author_worker(adapter, registry):
    return Worker(adapter, registry)


async def test_tests_are_authored_then_frozen(author_worker, provider, jail):
    """The mechanism, not the instruction: the implementer can read the tests
    and cannot write them."""
    provider.script(
        "mock-low",
        MockReply(
            finish_reason="tool_calls",
            tool_calls=[{
                "name": "write_file",
                "arguments": json.dumps({
                    "path": "tests/test_acceptance_add_exponential_backoff_to_the_uploader.py",
                    "content": "def test_retries():\n    assert False\n",
                }),
            }],
        ),
        MockReply(content="written"),
    )
    result = await author_acceptance_tests(author_worker, _spec(), jail, task_id="task_0001")

    assert result.written and result.frozen
    assert (jail.root / result.path).is_file()
    with pytest.raises(GuardDenied, match="frozen"):
        jail.resolve_for_write(result.path)


async def test_the_implementer_can_still_read_the_tests(author_worker, provider, jail):
    path = "tests/test_acceptance_add_exponential_backoff_to_the_uploader.py"
    provider.script(
        "mock-low",
        MockReply(finish_reason="tool_calls", tool_calls=[{
            "name": "write_file",
            "arguments": json.dumps({"path": path, "content": "def test_x(): pass\n"}),
        }]),
        MockReply(content="done"),
    )
    await author_acceptance_tests(author_worker, _spec(), jail, task_id="task_0001")

    assert jail.resolve(path)  # readable
    with pytest.raises(GuardDenied):
        jail.resolve_for_write(path)


async def test_the_author_is_told_not_to_implement(author_worker, provider, jail):
    provider.script("mock-low", MockReply(content="ok"))
    await author_acceptance_tests(author_worker, _spec(), jail)

    brief = provider.calls[0]["messages"][-1]["content"]
    assert "not implementing" in brief
    assert "cannot edit them" in brief


async def test_authorship_uses_the_cheap_tier_by_default(author_worker, provider, jail):
    """Turning criteria into a test file is transcription, not judgement."""
    provider.script("mock-low", MockReply(content="ok"))
    await author_acceptance_tests(author_worker, _spec(), jail)
    assert provider.calls[0]["model"] == "mock-low"


async def test_authorship_can_be_disabled(author_worker, provider, jail):
    result = await author_acceptance_tests(
        author_worker, _spec(), jail, policy=ConductorPolicy(test_authorship=Authorship.OFF)
    )
    assert not result.written
    assert provider.calls == []


async def test_no_criteria_means_nothing_to_author(author_worker, provider, jail):
    """Freezing an absent file would deny a path the implementer may legitimately
    need to create."""
    result = await author_acceptance_tests(author_worker, _spec(acceptance=[]), jail)
    assert not result.frozen
    assert "no acceptance criteria" in result.note
    assert jail.frozen == frozenset()


def test_the_test_path_is_derived_deterministically():
    assert default_test_path(_spec()) == default_test_path(_spec())
    assert default_test_path(_spec()).startswith("tests/test_acceptance_")


def test_an_existing_suite_can_be_frozen_too(jail):
    """An implementer that can edit the repo's own tests can pass by deleting
    them."""
    (jail.root / "tests").mkdir()
    (jail.root / "tests" / "test_existing.py").write_text("def test_a(): pass\n", encoding="utf-8")

    assert freeze_existing(jail, "tests/test_existing.py", "tests/absent.py") == [
        "tests/test_existing.py"
    ]
    with pytest.raises(GuardDenied):
        jail.resolve_for_write("tests/test_existing.py")


# ================================= Slot 38 — plan-vs-directive rationale


def test_a_faithful_plan_passes():
    check = check_plan(DIRECTIVE, _spec())
    assert check.ok
    assert "backoff" in check.shared_terms


def test_a_plan_with_no_goal_is_refused():
    check = check_plan(DIRECTIVE, _spec(goal="   "))
    assert not check.ok
    assert "states no goal" in check.problems[0]


def test_straying_outside_declared_scope_is_refused():
    check = check_plan(DIRECTIVE, _spec(artifacts=["src/billing.py"]), allowed_paths=["src/uploader.py"])
    assert not check.ok
    assert "outside the declared scope" in check.problems[0]


def test_a_plan_contradicting_its_own_forbidden_list_is_refused():
    check = check_plan(DIRECTIVE, _spec(goal="rewrite the billing module", forbidden=["billing module"]))
    assert not check.ok


def test_a_plan_with_no_acceptance_criteria_is_refused():
    """Found on the first live run: a conductor emitted `acceptance: []`, the
    authorship step had nothing to write, no test file was frozen, and the
    implementer wrote both the code and the tests it was graded by. pytest
    passed and the task reported success.

    Empty criteria do not merely leave the gate vague — they silently disable
    the separation that makes the gate mean anything, while still reporting a
    pass. This was a warning; it is a refusal now.
    """
    check = check_plan(DIRECTIVE, _spec(acceptance=[]))
    assert not check.ok
    assert any("bypassed" in p for p in check.problems)


def test_the_conductor_is_told_criteria_are_mandatory():
    """The guard refuses an empty list; the prompt has to ask for a non-empty one
    or every task burns a repair round discovering the same thing."""
    from aop.operator import CONDUCTOR_INSTRUCTIONS

    assert "REQUIRED" in CONDUCTOR_INSTRUCTIONS
    assert "must not be empty" in CONDUCTOR_INSTRUCTIONS


def test_low_vocabulary_overlap_warns_but_does_not_refuse():
    """'Make it reliable' and 'add exponential backoff' can share almost no words
    and still be the same task, so this cannot be a hard gate."""
    check = check_plan("make the uploader reliable",
                       _spec(goal="add exponential backoff", acceptance=["it retries"]))
    assert check.ok


def test_the_stated_rationale_never_grants_approval():
    """A model comparing its own plan against the directive is grading itself:
    useful for audit, worthless as enforcement."""
    record = record_rationale(
        "task_0001", DIRECTIVE, _spec(goal="  "),
        stated="This plan follows the directive exactly and is fully compliant.",
    )
    assert not record.trustworthy
    assert "fully compliant" in record.stated


def test_the_rationale_is_recorded_verbatim_for_audit():
    record = record_rationale("task_0001", DIRECTIVE, _spec(), stated="I kept the scope narrow.")
    assert record.stated == "I kept the scope narrow."
    assert record.check.ok


# ================================= Slots 39, 40 — features and routing


def test_features_are_deterministic():
    assert extract(_spec()) == extract(_spec())


def test_every_declared_feature_is_produced():
    assert set(extract(_spec())) == set(FEATURE_NAMES)


def test_the_vector_is_ordered_and_fixed_length():
    """A classifier needs stable positions; a vector whose length depends on its
    input is not one a model can consume."""
    vector = to_vector(extract(_spec()))
    assert len(vector) == len(FEATURE_NAMES)
    assert all(isinstance(v, float) for v in vector)


def test_a_row_stored_before_a_feature_existed_still_loads():
    assert to_vector({"goal_chars": 10.0}) [FEATURE_NAMES.index("goal_chars")] == 10.0


def test_unknown_features_are_dropped_not_appended():
    assert len(to_vector({"invented_later": 1.0})) == len(FEATURE_NAMES)


def test_difficulty_is_one_hot_not_ordinal():
    """Encoding it 0/1/2 would tell a linear model that hard is twice medium,
    which is not a claim anyone is making."""
    hard = extract(_spec(difficulty_hint=Difficulty.HARD))
    assert (hard["difficulty_hard"], hard["difficulty_medium"]) == (1.0, 0.0)


def test_keyword_signals_fire():
    f = extract(_spec(goal="fix the race condition causing a deadlock"))
    assert f["kw_concurrency"] == 1.0
    assert f["kw_debug"] == 1.0


def test_simple_boilerplate_routes_cheap(registry):
    decision = RuleRouter(registry).route(
        _spec(goal="scaffold a stub module", difficulty_hint=Difficulty.SIMPLE,
              acceptance=["the file exists"], artifacts=["src/stub.py"])
    )
    assert decision.role is Role.LOW


def test_hard_design_work_routes_expensive(registry):
    decision = RuleRouter(registry).route(
        _spec(
            goal="decide the concurrency architecture and fix the deadlock",
            difficulty_hint=Difficulty.HARD,
            acceptance=[f"criterion {i}" for i in range(6)],
            artifacts=[f"src/mod{i}.py" for i in range(6)],
        )
    )
    assert decision.role is Role.MAX


def test_ordinary_work_routes_to_the_default_tier(registry):
    decision = RuleRouter(registry).route(_spec())
    assert decision.role is Role.HIGH


def test_modality_overrides_difficulty(registry):
    """A pixel-bound task cannot go to a text-only tier however hard it is."""
    decision = RuleRouter(registry).route(
        _spec(
            goal="decide the architecture from this screenshot and fix the deadlock",
            difficulty_hint=Difficulty.HARD, needs_pixels=True,
            artifacts=[f"src/mod{i}.py" for i in range(6)],
        )
    )
    assert decision.desired is Role.MAX
    assert decision.role is Role.HIGH
    assert decision.modality_overrode
    assert "text-only" in decision.rationale


def test_every_decision_carries_its_evidence(registry):
    """A router decision nobody can explain is worse than a rule."""
    decision = RuleRouter(registry).route(_spec(difficulty_hint=Difficulty.HARD))
    assert decision.rationale
    assert decision.features
    assert "hard" in decision.rationale


def test_routing_is_deterministic(registry):
    router = RuleRouter(registry)
    assert router.route(_spec()).role is router.route(_spec()).role


def test_the_score_stays_in_range(registry):
    router = RuleRouter(registry)
    extreme = _spec(
        goal="design debug concurrency auth research " * 20,
        difficulty_hint=Difficulty.HARD,
        acceptance=[f"c{i}" for i in range(20)],
        constraints=[f"k{i}" for i in range(10)],
        artifacts=[f"m{i}.py" for i in range(20)],
    )
    assert 0.0 <= router.route(extreme).score <= 1.0
    assert 0.0 <= router.route(_spec(goal="x", difficulty_hint=Difficulty.SIMPLE)).score <= 1.0


def test_explain_is_human_readable(registry):
    text = RuleRouter(registry).explain(_spec(difficulty_hint=Difficulty.HARD))
    assert "score" in text and "features:" in text
