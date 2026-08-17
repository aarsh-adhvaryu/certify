"""The eval suite: representative tasks, graded by the verifier gate.

Spec §3.1 calls for this — "keep a saved suite of representative tasks and run
any candidate model through it, letting the verifier gate grade pass-rate and
cost against the incumbent". It was originally the last slot in the plan. It
belongs near the front instead, because it is the instrument that answers which
model should fill which role, and every hour spent arguing from benchmark tables
is an hour not spent measuring.

**A task is a directive plus a fixture, not a prompt.** Each one names a
workspace to copy in, the directive to give the operator, and what must be true
afterwards. The gate decides pass or fail; nothing here inspects the model's
reasoning or grades its prose, because a suite that scored explanations would
measure the wrong thing.

**Fixtures are copied, never referenced.** A suite that ran against a real
checkout would let a model being prompt-tuned edit work you care about, and would
also make the second run start from the first run's leftovers.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

from pydantic import Field

from aop.core.schemas import Difficulty, Strict

SUITE_VERSION = 1


class SuiteError(Exception):
    pass


class EvalTask(Strict):
    """One representative task."""

    id: str
    directive: str
    """Given to the operator verbatim, exactly as a user would type it."""

    acceptance: list[str] = Field(default_factory=list)
    """Stated for the record. The conductor is expected to derive equivalents;
    if it consistently does not, that is itself a finding about the conductor."""

    fixture: str | None = None
    """Directory under the suite's ``fixtures/`` to copy into the workspace."""

    difficulty: Difficulty = Difficulty.MEDIUM
    """The suite author's expectation. Compared against what the router chose —
    a systematic gap is a router problem, not a model problem."""

    needs_pixels: bool = False
    expect_pass: bool = True
    """False for tasks that *should* be refused or handed back. A suite of only
    achievable work cannot detect a model that agrees to anything."""

    tags: list[str] = Field(default_factory=list)
    timeout_seconds: float = 600.0

    def summary(self) -> str:
        return f"{self.id} [{self.difficulty.value}] {self.directive[:60]}"


class EvalSuite(Strict):
    version: int = SUITE_VERSION
    name: str
    description: str = ""
    tasks: list[EvalTask] = Field(default_factory=list)
    root: Path | None = None

    def __len__(self) -> int:
        return len(self.tasks)

    def by_id(self, task_id: str) -> EvalTask:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise SuiteError(f"no task {task_id!r} in suite {self.name!r}")

    def filter(self, *, tag: str | None = None, difficulty: Difficulty | None = None) -> list[EvalTask]:
        chosen = self.tasks
        if tag:
            chosen = [t for t in chosen if tag in t.tags]
        if difficulty:
            chosen = [t for t in chosen if t.difficulty is difficulty]
        return chosen

    @property
    def difficulty_mix(self) -> dict[str, int]:
        mix: dict[str, int] = {}
        for task in self.tasks:
            mix[task.difficulty.value] = mix.get(task.difficulty.value, 0) + 1
        return mix

    # -- loading -----------------------------------------------------------

    @classmethod
    def load(cls, path: Path | str) -> EvalSuite:
        path = Path(path)
        if not path.is_file():
            raise SuiteError(f"suite not found: {path}")

        try:
            with path.open("rb") as handle:
                payload = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise SuiteError(f"{path}: malformed TOML — {exc}") from exc

        version = payload.get("version", SUITE_VERSION)
        if version != SUITE_VERSION:
            raise SuiteError(
                f"{path}: suite version {version} is not readable by this build "
                f"(expected {SUITE_VERSION})"
            )

        try:
            suite = cls(
                version=version,
                name=payload.get("name", path.stem),
                description=payload.get("description", ""),
                tasks=[EvalTask(**t) for t in payload.get("task", [])],
                root=path.parent,
            )
        except Exception as exc:  # noqa: BLE001 - report the file, not a stack
            raise SuiteError(f"{path}: {exc}") from exc

        ids = [t.id for t in suite.tasks]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise SuiteError(f"{path}: duplicate task ids: {', '.join(sorted(duplicates))}")
        if not suite.tasks:
            raise SuiteError(f"{path}: a suite with no tasks measures nothing")

        return suite

    # -- fixtures ----------------------------------------------------------

    def fixture_path(self, task: EvalTask) -> Path | None:
        if task.fixture is None or self.root is None:
            return None
        return self.root / "fixtures" / task.fixture

    def stage(self, task: EvalTask, workspace: Path) -> Path:
        """Copy this task's fixture into a clean workspace.

        The workspace is emptied first. Otherwise the second candidate starts
        from whatever the first one left behind, and the comparison silently
        stops being like-for-like.
        """
        workspace = Path(workspace)
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)

        source = self.fixture_path(task)
        if source is None:
            return workspace
        if not source.is_dir():
            raise SuiteError(f"task {task.id!r} names a missing fixture: {source}")

        shutil.copytree(source, workspace, dirs_exist_ok=True)
        return workspace
