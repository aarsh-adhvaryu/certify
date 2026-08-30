"""Freezing the criteria.

The failure this stops: an agent writes real code, writes the test that grades
it, passes its own test, and reports success. Nothing was fabricated, so nothing
structural is detectable. The only defence is that the criteria were fixed
before the implementation began and cannot be edited afterwards.
"""

from __future__ import annotations

import pytest

from certify.criteria import freeze_existing
from certify.guards import GuardDenied, PathJail


@pytest.fixture
def jail(tmp_path) -> PathJail:
    root = tmp_path / "workspace"
    root.mkdir()
    return PathJail(root)


def test_an_existing_suite_can_be_frozen(jail):
    """A repository's own tests are as much the grading standard as anything
    written for one task, and an implementer that can edit them can pass by
    deleting them."""
    (jail.root / "tests").mkdir()
    (jail.root / "tests" / "test_existing.py").write_text(
        "def test_a(): pass\n", encoding="utf-8"
    )

    assert freeze_existing(jail, "tests/test_existing.py", "tests/absent.py") == [
        "tests/test_existing.py"
    ]
    with pytest.raises(GuardDenied):
        jail.resolve_for_write("tests/test_existing.py")


def test_an_absent_path_is_skipped_not_refused(jail):
    """Failing the whole run because one named file is missing would be
    over-refusal in the place it is least affordable."""
    assert freeze_existing(jail, "tests/nothing_here.py") == []


def test_a_frozen_file_stays_readable(jail):
    """Freezing gives the agent something it can read and cannot rewrite. An
    agent that cannot see what it is graded against is being set up to fail."""
    (jail.root / "criteria.py").write_text("def test_x(): pass\n", encoding="utf-8")
    freeze_existing(jail, "criteria.py")

    assert jail.resolve("criteria.py").read_text(encoding="utf-8")
    with pytest.raises(GuardDenied):
        jail.resolve_for_write("criteria.py")


def test_the_freeze_does_not_survive_the_process(jail):
    """Documents the gap E.1 closes rather than leaving it to be rediscovered.

    PathJail holds its frozen set in memory, on the instance. `certify begin`
    and `certify verify` are separate processes, so a freeze taken by the first
    does not exist for the second — the containment is real within one run and
    absent across two.
    """
    (jail.root / "criteria.py").write_text("def test_x(): pass\n", encoding="utf-8")
    freeze_existing(jail, "criteria.py")

    reopened = PathJail(jail.root)
    assert reopened.frozen == frozenset()
    reopened.resolve_for_write("criteria.py")  # no denial: the freeze is gone
