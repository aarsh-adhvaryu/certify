"""Freezing the criteria, so the thing being graded cannot write its own grade.

This is one of the two checks that are independently ours — the other is
``refusal.falsifiability``. Neither needs a code graph, and both survive every
other part of this tool disappearing.

The failure it exists to stop is not hypothetical and not exotic. An agent writes
real code in real files, writes the test that grades it, passes its own test, and
reports success. Nothing was fabricated, so nothing structural is detectable. The
only defence is that the criteria were fixed by a *different actor* before the
implementation began, and cannot be edited afterwards.

The freeze itself lives on ``PathJail`` rather than here, and that placement is
load-bearing: a check inside a write tool gets re-opened by the next tool that
forgets it, and "every tool remembered" is not a property you can test. The guard
is the one place every write must pass through.

**Known gap, closed in E.1.** ``PathJail`` holds its frozen set in memory, on the
instance. ``certify begin`` and ``certify verify`` are separate processes, so a
freeze taken by the first does not exist for the second. Persisting it is the
session record's job.

E.2 adds the criteria sources in priority order — user-supplied first (free,
deterministic, the documented CI path), graph-derived second (F), one cheap model
call third (E.7, an opt-in extra and out of v1). When none is available, ``begin``
says so plainly and runs advisory. It never silently pretends to gate.
"""

from __future__ import annotations

from certify.guards.pathjail import PathJail


def freeze_existing(jail: PathJail, *paths: str) -> list[str]:
    """Freeze criteria files that already exist — a repository's own suite.

    A project's existing tests are as much the grading standard as anything
    written for one task, and an implementer that can edit them can pass by
    deleting them.

    Returns the paths actually frozen. A path that does not exist is skipped
    rather than refused: freezing is a containment step, and failing the whole
    run because one named file is absent would be over-refusal in the place it
    is least affordable.
    """
    frozen = []
    for path in paths:
        if (jail.root / path).exists():
            jail.freeze(path)
            frozen.append(path)
    return frozen
