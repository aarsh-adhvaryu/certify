"""Slot 37 — test-authorship separation.

The hole this closes: if the worker writes both the implementation and the tests
it is graded by, the gate is theater. A model that cannot solve the task can
always write tests that pass, and it will — not by cheating, but because it
writes tests for the behaviour it actually implemented.

The fix is structural rather than instructional. Telling a model "write honest
tests" is a request; this is a mechanism:

1. The conductor emits acceptance criteria as part of the spec.
2. A **separate worker call** turns those criteria into a real test file. Cheap
   by default — transcription, not judgement.
3. The file is **frozen at the path-guard level** before the implementer is
   dispatched. It can read it and cannot write it.

Step three is why the freeze lives on ``PathJail`` and not inside ``write_file``:
a check in one tool is re-opened by the next tool that forgets it, and "every
tool remembered" is not a property anything can test.
"""

from __future__ import annotations

from pathlib import Path

from aop.context.assembler import ContextAssembler
from aop.core.config import ConductorPolicy, Authorship
from aop.core.schemas import Role, Strict, TaskSpec
from aop.execution.tools import build_toolbox
from aop.execution.worker import Worker
from aop.guards.pathjail import PathJail
from aop.registry.adapter import Message

AUTHOR_BRIEF = """\
Write the acceptance tests for the task below. You are not implementing it.

Rules:
- Write pytest tests that fail against the current code and pass only once the
  criteria genuinely hold.
- Test the stated behaviour, not an implementation you have in mind.
- Do not write, modify, or stub the implementation itself.
- Write the tests to the file named below using write_file, then stop.

Someone else implements against these tests and cannot edit them, so a test that
is wrong here blocks the work rather than being quietly fixed later.
"""


class AuthoredTests(Strict):
    path: str
    written: bool
    frozen: bool
    author_role: Role | None = None
    cost_usd: str = "0"
    note: str = ""


def default_test_path(spec: TaskSpec) -> str:
    """Where the acceptance tests go, derived from the spec deterministically."""
    stem = "".join(c if c.isalnum() else "_" for c in spec.goal.lower()).strip("_")
    return f"tests/test_acceptance_{stem[:40] or 'task'}.py"


async def author_acceptance_tests(
    worker: Worker,
    spec: TaskSpec,
    jail: PathJail,
    *,
    policy: ConductorPolicy | None = None,
    task_id: str | None = None,
    test_path: str | None = None,
) -> AuthoredTests:
    """Write the acceptance tests, then freeze them.

    Returns without freezing anything when authorship is off or the spec states
    no criteria — freezing an absent file would deny a path the implementer might
    legitimately need to create.
    """
    policy = policy or ConductorPolicy()
    path = test_path or default_test_path(spec)

    if policy.test_authorship is Authorship.OFF:
        return AuthoredTests(
            path=path, written=False, frozen=False, note="authorship disabled by policy"
        )
    if not spec.acceptance:
        return AuthoredTests(
            path=path,
            written=False,
            frozen=False,
            note="spec states no acceptance criteria, so there is nothing to author",
        )

    brief = "\n".join(
        [
            AUTHOR_BRIEF,
            f"Test file to write: {path}",
            "",
            f"## Goal\n{spec.goal}",
            "## Acceptance criteria",
            *(f"- {c}" for c in spec.acceptance),
        ]
    )

    assembler = ContextAssembler(spec.goal, instructions="You author acceptance tests.")
    author_spec = spec.model_copy(
        update={"goal": brief, "acceptance": [], "artifacts": [path]}
    )

    result = await worker.run(
        policy.test_author_role,
        author_spec,
        assembler,
        build_toolbox(jail),
        task_id=task_id,
    )

    written = (jail.root / path).is_file()
    if written:
        jail.freeze(path)

    return AuthoredTests(
        path=path,
        written=written,
        frozen=written,
        author_role=policy.test_author_role,
        cost_usd=str(result.cost_usd),
        note="" if written else "the author did not produce the test file",
    )


def freeze_existing(jail: PathJail, *paths: str) -> list[str]:
    """Freeze test files that already exist — a repository's own suite.

    Its tests are as much the grading standard as newly authored ones, and an
    implementer that can edit them can pass by deleting them.
    """
    frozen = []
    for path in paths:
        if (jail.root / path).exists():
            jail.freeze(path)
            frozen.append(path)
    return frozen
