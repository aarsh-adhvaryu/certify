"""The definition of a finished slot. Run this, not just pytest.

Every real bug in this project's history passed the unit suite first, and three
of them were the same shape: a component green in isolation, wired to nothing.
The suspend/resume mechanism was consumed by nothing at all. The write hook was
built, unit-tested, and never passed to the SDK. The shell bypass defeated
sixteen green containment tests with `echo >`.

More unit tests do not catch that shape, because all three had unit tests. So
this script checks the things pytest structurally cannot:

  1. the suite is green
  2. the end-to-end pipeline actually ran, on a real repository
  3. every module imports
  4. **no module is consumed only by its own tests** — the orphan check
  5. known holes are still marked, and none has silently started passing

Exit code is 0 only if every check passes. Run it before calling a slot done:

    python scripts/slot_check.py
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "certify"
TESTS = ROOT / "tests"
PIPELINE = TESTS / "test_pipeline.py"

#: Modules that will always be consumed from outside src/, by design.
#: Every entry needs a reason. This list is where an orphan check goes to die,
#: so it stays short and each line has to argue for itself.
EXEMPT: dict[str, str] = {
    "certify.hosts.claude_code": (
        "a host adapter — the core must not depend on a host, so nothing in src/ "
        "may import it. Note this is NOT the same as it being wired: E.3 must "
        "assert the host was handed the hook, which is the exact thing its unit "
        "tests cannot do."
    ),
}

#: Orphaned today, with the slot that ends it. **Not exemptions — a countdown.**
#:
#: These are the product's own surface, and they are consumed by nothing but
#: their own tests because there is no CLI yet. That is the honest current state
#: of this repository and the check reports it rather than hiding it.
#:
#: A module here that STOPS being orphaned also fails the check, so the list has
#: to be pruned deliberately. Same discipline as a strict xfail: the day the
#: situation improves, someone has to notice.
PENDING: dict[str, str] = {
    # The product's own surface. Nothing calls these because there is no CLI —
    # which is the single most honest summary of where this repository is.
    "certify.refusal": "A.1 — `certify check` is the first caller",
    "certify.session": "E.1 — the session record",
    "certify.criteria": "E.2 — `certify begin` freezes through it",
    "certify.measure": "C.2 — the scorer gets a CLI surface",
    "certify.core.journal": "E.6 — the verdict writes a journal line",
    "certify.core.lifecycle": "E.1 — begin/verify drive the state machine",
    # Re-export surfaces. They will be reached by the CLI and by host wrappers,
    # both of which are outside src/ today.
    "certify.core": "A.1 — the CLI imports through it",
    "certify.guards": "A.1",
    "certify.verify": "E.4 — `certify verify` runs the gate",
    "certify.backends": "E.4 — the gate needs a runner",
    "certify.hosts": "E.3 — host wiring",
}


def _fail(msg: str) -> str:
    return f"\033[31mFAIL\033[0m {msg}"


def _ok(msg: str) -> str:
    return f"\033[32m ok \033[0m {msg}"


def _warn(msg: str) -> str:
    return f"\033[33mwarn\033[0m {msg}"


# ----------------------------------------------------------------- the checks


def check_suite() -> tuple[bool, str]:
    """The whole suite, green."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    lines = (proc.stdout or proc.stderr).strip().splitlines()
    summary = next(
        (ln for ln in reversed(lines) if "passed" in ln or "failed" in ln or "error" in ln),
        lines[-1] if lines else "no output",
    )
    return proc.returncode == 0, summary.strip()


def check_pipeline_ran() -> tuple[bool, str]:
    """The end-to-end file exists, has real tests, and passed.

    Checked separately from the suite because "delete the slow test" is the
    cheapest way to make this whole discipline evaporate, and a suite that is
    green because a file is missing is exactly the failure being guarded.
    """
    if not PIPELINE.is_file():
        return False, f"{PIPELINE.name} is missing — the end-to-end check is gone"

    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    count = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name.startswith("test_")
    )
    if count < 5:
        return False, f"{PIPELINE.name} has only {count} tests — that is not end to end"

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(PIPELINE), "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return proc.returncode == 0, f"{count} end-to-end tests on a real repository"


def check_everything_imports() -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-c",
         "import importlib,pkgutil,certify;"
         "mods=[m.name for m in pkgutil.walk_packages(certify.__path__,'certify.')];"
         "[importlib.import_module(m) for m in mods];"
         "print(len(mods))"],
        cwd=ROOT, capture_output=True, text=True,
        env={**_env(), "PYTHONPATH": str(ROOT / "src")},
    )
    if proc.returncode != 0:
        return False, (proc.stderr or "").strip().splitlines()[-1]
    return True, f"{proc.stdout.strip()} modules import"


def check_no_orphans() -> tuple[bool, str]:
    """Is every module consumed by something other than its own tests?

    **This is the check that would have caught suspend/resume.** A module only
    ever imported by `tests/` is green and wired to nothing: the tests construct
    it, call it, and prove it works, while no path a user can reach goes near it.

    Test imports deliberately do not count as consumers. That is the whole
    point — being tested is precisely what made the dead code look alive.
    """
    modules: set[str] = set()
    importers: dict[str, set[str]] = defaultdict(set)

    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC.parent).with_suffix("")
        is_init = rel.name == "__init__"
        name = ".".join(rel.parts[:-1] if is_init else rel.parts)
        # Package __init__ files are judged too. A re-export surface that nothing
        # outside reaches for is still a surface nobody uses, and exempting them
        # is how the check quietly stops noticing whole subtrees.
        if name != "certify":
            modules.add(name)
        for imported in _imports_of(path):
            importers[imported].add(name)

    orphaned = {
        name
        for name in modules
        if name not in EXEMPT and not {c for c in importers.get(name, set()) if c != name}
    }

    unexpected = sorted(orphaned - PENDING.keys())
    resolved = sorted(PENDING.keys() - orphaned)

    problems = []
    if unexpected:
        listed = "\n       ".join(unexpected)
        problems.append(
            "imported by nothing in src/ — green in isolation, wired to nothing:\n"
            f"       {listed}\n"
            "       Wire it, delete it, or add it to PENDING with the slot that wires it."
        )
    if resolved:
        listed = "\n       ".join(f"{m}  ({PENDING[m]})" for m in resolved)
        problems.append(
            "no longer orphaned, so remove from PENDING:\n"
            f"       {listed}"
        )

    if problems:
        return False, "\n       ".join(problems)

    waiting = ", ".join(sorted(PENDING)) if PENDING else "none"
    return True, (
        f"{len(modules)} modules; {len(orphaned)} awaiting a caller as expected"
        + (f"\n       pending: {waiting}" if PENDING else "")
    )


def check_known_holes() -> tuple[bool, str]:
    """Strict xfails must still be marked, and must still be failing.

    `strict=True` means a hole that gets closed turns the suite red, so it is
    noticed and the marker is removed deliberately. This reports the count so a
    slot cannot quietly accumulate them.
    """
    holes = []
    for path in TESTS.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "xfail" in text and "strict=True" in text:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                    "xfail" in ast.dump(d) for d in node.decorator_list
                ):
                    holes.append(f"{path.name}::{node.name}")
    if holes:
        listed = "\n       ".join(holes)
        return True, f"{len(holes)} known hole(s), still marked:\n       {listed}"
    return True, "no known holes marked"


def _imports_of(path: Path) -> set[str]:
    """Every `certify.*` module a file imports, including via `from x import y`."""
    out: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names if a.name.startswith("certify"))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("certify"):
            out.add(node.module)
            # `from certify.core import state` imports certify.core.state too.
            out.update(f"{node.module}.{a.name}" for a in node.names)
    return out


def _env() -> dict[str, str]:
    import os

    return dict(os.environ)


CHECKS = [
    ("suite", check_suite),
    ("pipeline", check_pipeline_ran),
    ("imports", check_everything_imports),
    ("orphans", check_no_orphans),
    ("holes", check_known_holes),
]


def main() -> int:
    print("slot check — the definition of done\n")
    failed = []
    for name, fn in CHECKS:
        passed, detail = fn()
        print(f"{_ok(name) if passed else _fail(name)}  {detail}")
        if not passed:
            failed.append(name)

    print()
    if failed:
        print(_fail(f"slot is NOT done: {', '.join(failed)}"))
        return 1
    print(_ok("slot is done"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
