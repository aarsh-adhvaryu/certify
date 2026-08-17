"""Slot 28 — pruning.

Caching fixes cost; it does not fix attention dilution. As a task runs, the tail
fills with completed sub-tasks and stale observations, and the model's attention
gets spread across material that no longer matters. Pruning keeps only the active
frontier.

**Store before you drop.** Every message removed is written to memory first, and
the write is verified before the drop happens. Lossy deletion causes the worst
class of failure here — a step fails because something dropped three turns ago
turns out to have been relevant, and nothing in the transcript explains why.

**The summary is built from state, not written by a model.** It is assembled from
what the system already knows: how many turns were folded, which files were
touched, which verdicts were reached. Zero tokens, fully testable, and it cannot
invent. That last property is the point — a summary the conductor then plans
from is precisely the wrong place to introduce a hallucination, and a model
summarising its own failures is not a neutral witness to them.

The trade is honest: this reads more mechanically than prose would. The seam for
a model-written summary exists if that turns out to matter.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from aop.core.events import EventBus, LogLine
from aop.core.schemas import Strict
from aop.context.assembler import ContextAssembler
from aop.memory.store import MemoryKind, MemoryStore, SqliteMemoryStore
from aop.registry.adapter import Message

#: Rough characters-per-token. Only used to decide *when* to prune, never to
#: bill anything, so an approximation is fine and a tokeniser dependency is not
#: worth its weight.
CHARS_PER_TOKEN = 4

_PATH = re.compile(r"[\w./\\-]+\.(?:py|json|toml|md|txt|ts|js|yaml|yml)")


class PruneResult(Strict):
    pruned_messages: int
    stored_items: int
    summary: str
    tokens_before: int
    tokens_after: int

    @property
    def freed_tokens(self) -> int:
        return max(self.tokens_before - self.tokens_after, 0)


def estimate_tokens(messages: Sequence[Message]) -> int:
    return sum(len(m.content or "") for m in messages) // CHARS_PER_TOKEN


def summarise(messages: Sequence[Message]) -> str:
    """A deterministic account of what is being folded away.

    Built entirely from the messages themselves — counts, roles, file paths, and
    any verifier verdicts visible in the text. Same input, same summary.
    """
    if not messages:
        return ""

    by_role: dict[str, int] = {}
    files: list[str] = []
    rejections = 0

    for message in messages:
        by_role[message.role] = by_role.get(message.role, 0) + 1
        body = message.content or ""
        if "gate rejected" in body:
            rejections += 1
        for path in _PATH.findall(body):
            if path not in files:
                files.append(path)

    roles = ", ".join(f"{n} {role}" for role, n in sorted(by_role.items()))
    lines = [f"[pruned] {len(messages)} earlier messages ({roles})."]
    if files:
        shown = ", ".join(sorted(files)[:12])
        more = "" if len(files) <= 12 else f" (+{len(files) - 12} more)"
        lines.append(f"Files referenced: {shown}{more}.")
    if rejections:
        lines.append(f"Verifier rejections folded in: {rejections}.")
    lines.append("Full detail is retrievable from memory.")
    return " ".join(lines)


class Pruner:
    """Keeps the volatile tail from bloating, without losing anything."""

    def __init__(
        self,
        memory: MemoryStore,
        *,
        trigger_tokens: int = 12_000,
        keep_recent: int = 6,
        bus: EventBus | None = None,
    ) -> None:
        self._memory = memory
        self._trigger = trigger_tokens
        self._keep = keep_recent
        self._bus = bus

    def should_prune(self, assembler: ContextAssembler) -> bool:
        return estimate_tokens(assembler.tail) >= self._trigger

    async def prune(
        self, assembler: ContextAssembler, *, task_id: str | None = None, force: bool = False
    ) -> PruneResult:
        """Fold the older tail into memory, keeping the frontier.

        The prefix is not touched, so the cache survives a prune. Only the tail
        moves — which is the whole reason context is ordered this way.
        """
        tail = assembler.tail
        before = estimate_tokens(tail)

        if not force and not self.should_prune(assembler):
            return PruneResult(
                pruned_messages=0,
                stored_items=0,
                summary="",
                tokens_before=before,
                tokens_after=before,
            )

        keep = tail[-self._keep :] if self._keep else []
        drop = tail[: len(tail) - len(keep)]
        if not drop:
            return PruneResult(
                pruned_messages=0,
                stored_items=0,
                summary="",
                tokens_before=before,
                tokens_after=before,
            )

        # Store first. If this raises, nothing has been dropped and the context
        # is exactly as it was.
        items = [
            self._item(message, task_id)
            for message in drop
            if (message.content or "").strip()
        ]
        stored = await self._memory.write(*items) if items else 0
        if items and stored != len(items):
            raise RuntimeError(
                "memory refused some items; refusing to drop context that was "
                "not stored"
            )

        summary = summarise(drop)
        assembler.replace_tail([Message.user(summary), *keep])

        after = estimate_tokens(assembler.tail)
        if self._bus:
            self._bus.emit(
                LogLine,
                task_id=task_id,
                level="info",
                message="context pruned",
                detail={
                    "messages": str(len(drop)),
                    "stored": str(stored),
                    "freed_tokens": str(max(before - after, 0)),
                },
            )

        return PruneResult(
            pruned_messages=len(drop),
            stored_items=stored,
            summary=summary,
            tokens_before=before,
            tokens_after=after,
        )

    def _item(self, message: Message, task_id: str | None):
        text = f"[{message.role}] {message.content or ''}"
        tags = sorted(set(_PATH.findall(message.content or "")))
        if isinstance(self._memory, SqliteMemoryStore):
            return self._memory.new_item(
                text, task_id=task_id, kind=MemoryKind.PRUNED, tags=tags
            )

        # Stores that do not mint ids get one built the same way.
        from datetime import UTC, datetime
        from uuid import uuid4

        from aop.memory.store import MemoryItem

        return MemoryItem(
            item_id=f"mem_{uuid4().hex}",
            task_id=task_id,
            kind=MemoryKind.PRUNED,
            text=text,
            tags=tags,
            created_at=datetime.now(UTC),
        )
