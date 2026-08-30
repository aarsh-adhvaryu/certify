"""Slot 24 — the pytest gate.

The first real verifier and the one the whole coding path depends on. Runs the
suite through the execution backend, so it obeys the same jail and allowlist as
everything else and works identically on Windows or in WSL.

**Failure reason is the raw pytest output, trimmed but never paraphrased.** That
text is injected verbatim into the retry, and the assertion diff plus the file
and line number are the parts that actually let a model fix its own mistake. A
summary would throw away exactly those.

Three outcomes, deliberately not two:

* exit 0 → pass
* tests ran and failed → ``Verdict.failed``, a genuine verifier failure that may
  escalate and does train the router
* the suite could not run at all — pytest missing, collection error, timeout →
  ``Verdict.errored``. That is our tooling breaking, not the model being weak,
  so it retries at the same tier and never becomes a label.

Getting the third case wrong is the expensive one: a broken conftest would
otherwise escalate every task to the most expensive tier while teaching the
router that everything is hard.
"""

from __future__ import annotations

import re

from certify.backends.base import RunBackend, RunResult
from certify.core.schemas import Verdict
from certify.verify.base import Verifier, VerifierKind, VerifyContext

#: pytest's own exit codes.
EXIT_OK = 0
EXIT_TESTS_FAILED = 1
EXIT_INTERRUPTED = 2
EXIT_INTERNAL_ERROR = 3
EXIT_USAGE_ERROR = 4
EXIT_NO_TESTS = 5

_SUMMARY = re.compile(r"^=+ .*(passed|failed|error|no tests ran).* =+$", re.MULTILINE)
_COUNTS = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")

MAX_REASON_CHARS = 4000


class PytestGate(Verifier):
    """Runs a test suite and turns the result into a verdict."""

    name = "pytest"
    kind = VerifierKind.STATIC
    """Static in the scheduling sense: the orchestrator awaits it rather than
    parking the task. A suite slow enough to need suspension should be wrapped in
    a stateful check instead."""

    def __init__(
        self,
        backend: RunBackend,
        *,
        target: str = "tests",
        timeout: float = 300.0,
        extra_args: tuple[str, ...] = ("-q",),
    ) -> None:
        self._backend = backend
        self._target = target
        self._timeout = timeout
        self._extra = extra_args
        self.expected_seconds = min(timeout, 30.0)

    async def verify(self, ctx: VerifyContext) -> Verdict:
        target = ctx.target or self._target
        argv = ["python", "-m", "pytest", target, *self._extra]

        try:
            result = await self._backend.run(argv, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - the gate must never crash a task
            return Verdict.errored("pytest", f"could not run the suite: {exc}")

        return self._to_verdict(result)

    def _to_verdict(self, result: RunResult) -> Verdict:
        output = _trim(result.output)
        counts = dict(
            (kind, int(n)) for n, kind in _COUNTS.findall(result.output or "")
        )
        detail = {k: str(v) for k, v in counts.items()}

        if result.timed_out:
            return Verdict.errored(
                "pytest",
                f"the suite did not finish within {self._timeout:g}s — a hang is a "
                f"different problem from a failure\n\n{output}",
                duration_ms=result.duration_ms,
                **detail,
            )

        if result.exit_code == EXIT_OK:
            return Verdict.passed("pytest", duration_ms=result.duration_ms, **detail)

        if result.exit_code == EXIT_TESTS_FAILED and _actually_ran(result.output):
            return Verdict.failed(
                "pytest",
                reason=_focus(result.output) or output,
                duration_ms=result.duration_ms,
                **detail,
            )

        # Everything else means the suite could not run. Ours to fix, not the
        # model's — so it must not escalate a tier or write a training label.
        why = {
            EXIT_INTERRUPTED: "the run was interrupted",
            EXIT_INTERNAL_ERROR: "pytest hit an internal error",
            EXIT_USAGE_ERROR: "pytest was invoked incorrectly",
            EXIT_NO_TESTS: "no tests were collected",
            127: "python is not available on this backend",
        }.get(result.exit_code, f"pytest exited {result.exit_code}")
        if result.exit_code == EXIT_TESTS_FAILED:
            why = "pytest could not run — it produced no test results at all"
        return Verdict.errored(
            "pytest", f"{why}\n\n{output}", duration_ms=result.duration_ms, **detail
        )


def _actually_ran(output: str) -> bool:
    """Whether pytest got far enough to report on tests.

    Exit code 1 is not sufficient evidence that tests ran and failed. ``python -m
    pytest`` also exits 1 when the module is not installed, and that is the
    interpreter complaining, not a verdict. Treating it as a test failure would
    escalate the task to a more expensive tier and write "this tier failed" into
    the router's training set — for a missing dependency.

    A real run always prints either a summary banner or a count, so requiring one
    of those is a cheap and reliable discriminator.
    """
    return bool(_SUMMARY.search(output or "") or _COUNTS.search(output or ""))


def _trim(text: str) -> str:
    """Keep the end, which is where pytest puts the failures and the summary."""
    if len(text) <= MAX_REASON_CHARS:
        return text
    return "...(truncated)...\n" + text[-MAX_REASON_CHARS:]


def _focus(output: str) -> str:
    """Prefer the failure section over the whole log.

    Everything from the first ``FAILURES`` banner onward, which is the assertion
    diffs and the summary — the part a model can act on.
    """
    marker = output.find("=== FAILURES ===")
    if marker == -1:
        marker = output.find("FAILURES")
    if marker == -1:
        return _trim(output)
    return _trim(output[marker:])
