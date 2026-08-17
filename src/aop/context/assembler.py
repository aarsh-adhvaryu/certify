"""Slot 26 — context assembly.

Context is ordered ``[ stable cached prefix | volatile tail ]`` and the split is
enforced by the type rather than by everyone remembering (spec §8.3).

The prefix holds the instructions and the immutable ``<Directive>``. It is frozen
when the task starts, hashed, and never rewritten in place. The tail holds the
active frontier — the churn.

**Why this matters twice.** Keeping churn out of the prefix preserves the cache
discount, which is the single biggest structural saving available. It is also the
latency defence: an unchanged prefix lets the provider skip prefill, which takes
retry time-to-first-token from roughly three seconds to roughly two hundred
milliseconds. A retry is exactly when a user is least willing to wait.

**Retry appends. It does not rebuild.** ``append_failure`` puts the verifier's
reason in the tail and leaves the prefix byte-identical. That is why there is no
public method that quietly re-renders a prompt: someone would eventually
"helpfully" re-inline the failure into the system message, and it would cost the
discount and the prefill silently, on every retry, forever.

Rebuilding is still sometimes correct — pruning necessarily rewrites the prefix,
and so does a genuine re-plan. So :meth:`rebuild_prefix` exists, is explicit, and
emits an event. The rule is not "never rebuild"; it is "never rebuild as a side
effect".
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence

from aop.core.events import EventBus, LogLine
from aop.core.schemas import Strict, hash_directive
from aop.registry.adapter import Message


class ContextError(Exception):
    pass


class PrefixMutated(ContextError):
    """The stable prefix changed without an explicit rebuild.

    Almost always means something re-rendered the prompt rather than appending
    to the tail — the exact mistake that silently costs the cache discount.
    """


class ContextStats(Strict):
    prefix_messages: int
    tail_messages: int
    prefix_chars: int
    tail_chars: int
    rebuilds: int

    @property
    def cacheable_fraction(self) -> float:
        total = self.prefix_chars + self.tail_chars
        return (self.prefix_chars / total) if total else 0.0


class ContextAssembler:
    """Owns both halves of a task's context."""

    def __init__(
        self,
        directive: str,
        instructions: str = "",
        *,
        bus: EventBus | None = None,
        task_id: str | None = None,
    ) -> None:
        self._directive = directive
        self._directive_hash = hash_directive(directive)
        self._instructions = instructions
        self._bus = bus
        self._task_id = task_id

        self._prefix: list[Message] = self._render_prefix()
        self._prefix_hash = self._hash(self._prefix)
        self._tail: list[Message] = []
        self._rebuilds = 0

    # -- the prefix --------------------------------------------------------

    def _render_prefix(self) -> list[Message]:
        """The stable head: instructions, then the directive verbatim.

        The directive is placed last in the system message and quoted exactly as
        the user wrote it, so a later comparison can catch the conductor
        restating intent rather than following it.
        """
        parts = []
        if self._instructions:
            parts.append(self._instructions.strip())
        parts.append(
            "<Directive>\n"
            f"{self._directive}\n"
            "</Directive>\n"
            "This directive is immutable. Every plan is judged against it as "
            "written, not as remembered."
        )
        return [Message.system("\n\n".join(parts))]

    @property
    def directive(self) -> str:
        return self._directive

    @property
    def directive_hash(self) -> str:
        return self._directive_hash

    @property
    def prefix_hash(self) -> str:
        return self._prefix_hash

    @staticmethod
    def _hash(messages: Sequence[Message]) -> str:
        blob = "\x00".join(f"{m.role}:{m.content or ''}" for m in messages)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def verify_prefix(self) -> None:
        """Raise if the prefix has drifted since it was frozen."""
        if self._hash(self._prefix) != self._prefix_hash:
            raise PrefixMutated(
                "the cached prefix changed without an explicit rebuild; "
                "retries must append to the tail"
            )

    # -- the tail ----------------------------------------------------------

    def append(self, *messages: Message) -> None:
        """Add to the volatile tail."""
        self._tail.extend(messages)

    def append_failure(self, reason: str, *, verifier: str = "verifier") -> None:
        """Put a verifier's reason into the tail, verbatim.

        Verbatim because the assertion diff, the file, and the line number are
        the parts a model can act on. It goes in the tail so the prefix stays
        cached — which is what makes the retry cheap *and* fast.
        """
        self._tail.append(
            Message.user(
                f"The {verifier} gate rejected the previous attempt.\n\n"
                f"{reason}\n\n"
                "Address this specific failure. The directive above is unchanged."
            )
        )

    def clear_tail(self) -> None:
        self._tail.clear()

    # -- rebuilding, explicitly -------------------------------------------

    def rebuild_prefix(self, instructions: str, *, reason: str) -> None:
        """Re-render the stable head on purpose.

        Legitimate after a prune or a re-plan. Costs the cache, so it is loud: it
        takes a reason, bumps a counter, and emits an event. If this fires on an
        ordinary retry, something is wrong.
        """
        self._instructions = instructions
        self._prefix = self._render_prefix()
        self._prefix_hash = self._hash(self._prefix)
        self._rebuilds += 1
        if self._bus:
            self._bus.emit(
                LogLine,
                task_id=self._task_id,
                level="info",
                message="context prefix rebuilt",
                detail={"reason": reason, "rebuilds": str(self._rebuilds)},
            )

    def replace_tail(self, messages: Iterable[Message]) -> None:
        """Swap the frontier wholesale. Used by the pruner (Slot 28)."""
        self._tail = list(messages)

    # -- output ------------------------------------------------------------

    def messages(self) -> list[Message]:
        """The full context, prefix first. Verifies the prefix on the way out."""
        self.verify_prefix()
        return [*self._prefix, *self._tail]

    @property
    def prefix(self) -> list[Message]:
        return list(self._prefix)

    @property
    def tail(self) -> list[Message]:
        return list(self._tail)

    @property
    def rebuilds(self) -> int:
        return self._rebuilds

    def stats(self) -> ContextStats:
        return ContextStats(
            prefix_messages=len(self._prefix),
            tail_messages=len(self._tail),
            prefix_chars=sum(len(m.content or "") for m in self._prefix),
            tail_chars=sum(len(m.content or "") for m in self._tail),
            rebuilds=self._rebuilds,
        )
