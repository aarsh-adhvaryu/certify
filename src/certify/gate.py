"""The boundary: a trusted manifest plus an untrusted worktree, in, one verdict out.

This is the composition root for verification, and the only place that knows the
order the checks happen in. Nothing here is a new rule — it is sequencing, and the
sequence is the substance.

## Why the order is the design

    1. load the manifest        cannot read it        -> UNVERIFIABLE
    2. check for tampering      criteria moved        -> TAMPERED
    3. check there is anything to check
                                no criteria           -> UNVERIFIABLE
    4. run the suite            it did not run        -> TOOL_ERROR
                                it ran and failed     -> FAIL
    5. check for tampering AGAIN
                                criteria moved        -> TAMPERED
    6.                          otherwise             -> PASS

**Tampering is checked before the suite** because a result computed against
criteria that already moved is not a weaker result, it is an uninterpretable one.
Running first and reporting "passed, but tampered" invites somebody to read the
pass.

**And again afterwards**, because between the two checks we deliberately execute
code from the repository under test. A suite that rewrites its own criteria mid
run would otherwise pass a before-check, weaken itself, and pass. The second
check costs one re-hash and closes a hole that would be very hard to find later.

**UNVERIFIABLE is not FAIL.** Reporting "your work is wrong" when the truth is "I
never had a way to tell" is the specific dishonesty this project exists to
remove. It is also not PASS, which is the tempting default when nothing objected.

## What "trusted" actually means here

This module cannot make a manifest trustworthy; it can only decline to pretend.
`verify()` reports which level it operated at, and the caller is expected to say
so out loud:

* an unsigned manifest inside the worktree — **tamper-evident**, and an agent
  that escapes containment can edit both the criteria and the record of them;
* an unsigned manifest outside the worktree — **tamper-resistant** against an
  agent that stays contained;
* a signed manifest, key held elsewhere — **trusted**, and the only one that is
  a guarantee rather than a strong hint.

In CI the manifest comes from a protected ref or a separate audit repository, and
the implementation branch never has write access to it. That is the arrangement
the strongest claim rests on, and it is a property of how the workflow is wired
rather than anything this file can enforce.
"""

from __future__ import annotations

from pathlib import Path

from certify.core.schemas import Strict, Verdict, VerdictStatus
from certify.manifest import Manifest, ManifestError, Tampering
from certify.verify.base import VerifyContext
from certify.verify.pytest_gate import PytestGate

VERIFIER = "certify"


class Trust(Strict):
    """How much the verdict is worth, reported rather than assumed.

    Carried on the result because the difference between these levels is
    invisible from inside a passing run, and a claim made at the wrong level is
    how a verification tool becomes the thing it was built to catch.
    """

    signed: bool = False
    """A signature was present and verified against a key we hold."""

    outside_worktree: bool = False
    """The manifest lives where an agent contained to the repository cannot write."""

    @property
    def level(self) -> str:
        if self.signed:
            return "trusted"
        if self.outside_worktree:
            return "tamper-resistant"
        return "tamper-evident"

    @property
    def caveat(self) -> str:
        """One line a caller can print verbatim. Never silently omitted."""
        if self.signed:
            return "manifest signed and verified"
        if self.outside_worktree:
            return (
                "manifest is unsigned but outside the worktree — resists an agent "
                "that stays contained, not one that escapes"
            )
        return (
            "manifest is unsigned and inside the worktree — changes are detectable, "
            "but anything that can edit the criteria can edit this record too"
        )


class Outcome(Strict):
    """A verdict, plus how much it is worth and what was actually checked."""

    verdict: Verdict
    trust: Trust
    tampering: Tampering | None = None
    criteria: list[str] = []

    @property
    def ok(self) -> bool:
        return self.verdict.ok

    def describe(self) -> str:
        lines = [f"{self.verdict.status.value.upper()}  ({self.trust.level})"]
        if self.verdict.reason:
            lines.append(f"  {self.verdict.reason.strip().splitlines()[0]}")
        if self.criteria:
            lines.append(f"  criteria: {', '.join(self.criteria)}")
        lines.append(f"  {self.trust.caveat}")
        return "\n".join(lines)


async def verify(
    root: Path,
    manifest_path: Path,
    *,
    gate: PytestGate,
    key: bytes | None = None,
    target: str = "tests",
    task_id: str = "verify",
) -> Outcome:
    """Grade a worktree against a manifest. See the module docstring for the order.

    `gate` is injected rather than constructed here so this stays sequencing.
    """
    root = Path(root).resolve()
    manifest_path = Path(manifest_path)
    trust = Trust(outside_worktree=root not in manifest_path.resolve().parents)

    # 1. A manifest we cannot read is not a failure of the work.
    try:
        manifest = Manifest.load(manifest_path)
    except ManifestError as exc:
        return Outcome(
            verdict=Verdict.unverifiable(VERIFIER, str(exc)),
            trust=trust,
        )

    criteria = [c.path for c in manifest.criteria]
    trust = trust.model_copy(
        update={"signed": bool(key) and manifest.verify_signature(key)}
    )

    # 2. Tampering, before anything is run.
    before = manifest.check(root, key=key)
    if not before.ok:
        return Outcome(
            verdict=Verdict.tampered(VERIFIER, before.summary),
            trust=trust,
            tampering=before,
            criteria=criteria,
        )

    # 3. Nothing to check against is not a pass and not a failure.
    if not manifest.criteria:
        return Outcome(
            verdict=Verdict.unverifiable(
                VERIFIER,
                "the manifest freezes no criteria, so there is nothing to grade "
                "against. Empty criteria do not make the gate vague — they "
                "disable it while still reporting a pass.",
            ),
            trust=trust,
            tampering=before,
        )

    # 4. Run it.
    verdict = await gate.verify(
        VerifyContext(task_id=task_id, workspace=root, target=target)
    )

    # 5. Tampering again — we just executed code from the repository under test.
    after = manifest.check(root, key=key)
    if not after.ok:
        return Outcome(
            verdict=Verdict.tampered(
                VERIFIER,
                f"criteria changed while the suite was running: {after.summary}. "
                f"The suite reported {verdict.status.value}; that result is not "
                "interpretable.",
            ),
            trust=trust,
            tampering=after,
            criteria=criteria,
        )

    return Outcome(verdict=verdict, trust=trust, tampering=after, criteria=criteria)
