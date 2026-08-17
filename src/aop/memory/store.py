"""Slot 27 — the memory store.

The shared blackboard, and the place the pruner puts things. Nothing else: this
is not a document library. What lands here are short operational fragments —
a completed sub-task and its verdict, an error trace, files touched, a decision
that was made and why.

**Retrieval is lexical, over SQLite FTS5.** That is a deliberate fit to the
workload rather than a compromise. What gets looked up here is filenames,
identifiers, test names, and error strings, which are exactly what lexical search
is good at. Writes happen on every prune, so per-write embedding cost would be a
recurring tax on the hot path, and FTS5 is already in the standard library.

The honest limit: no semantic matching. "auth bug" will not find "login failure".
:class:`MemoryStore` is an interface, so an embedding-backed implementation can be
dropped in later without touching the pruner.
"""

from __future__ import annotations

import abc
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path

from aop.core.ids import Clock, IdSource, SystemClock, UuidIds
from aop.core.schemas import Strict


class MemoryKind(str, Enum):
    PRUNED = "pruned"
    """Detail moved out of the active context."""

    DECISION = "decision"
    OBSERVATION = "observation"
    OUTCOME = "outcome"
    NOTE = "note"


class MemoryItem(Strict):
    item_id: str
    task_id: str | None = None
    kind: MemoryKind = MemoryKind.PRUNED
    text: str
    tags: list[str] = []
    created_at: datetime

    def searchable(self) -> str:
        return " ".join([self.text, *self.tags])


class MemoryStore(abc.ABC):
    """What the pruner needs and nothing more."""

    @abc.abstractmethod
    async def write(self, *items: MemoryItem) -> int: ...

    @abc.abstractmethod
    async def search(
        self, query: str, *, limit: int = 10, task_id: str | None = None
    ) -> list[MemoryItem]: ...

    @abc.abstractmethod
    async def get(self, item_id: str) -> MemoryItem | None: ...

    @abc.abstractmethod
    async def count(self) -> int: ...


#: FTS5 treats these as syntax. User text is quoted rather than stripped, so a
#: search for `assert x == 1` is a search for that string and not a parse error.
_FTS_SPECIAL = re.compile(r'[^\w\s]')


def _tokens(query: str) -> list[str]:
    return [t for t in _FTS_SPECIAL.sub(" ", query).split() if t]


def _match_queries(query: str) -> list[str]:
    """Safe FTS5 MATCH expressions, most precise first.

    Every token is double-quoted, which makes it a literal and neutralises FTS5's
    operators. Without that, a pruned traceback containing ``*`` or ``:`` would
    raise a syntax error from inside the memory layer — a failure with no
    relationship to anything the caller did wrong.

    Two queries are returned, and precision is tried before recall:

    1. **The whole query as an adjacent phrase.** ``uploader.py`` tokenises to
       ``uploader py``, and as a phrase that matches ``src/uploader.py`` while
       *not* matching ``src/parser.py``.
    2. **Any token.** Used only when the phrase found nothing.

    Skipping step one and going straight to OR is the obvious implementation and
    it is wrong: a search for one filename would return every file sharing its
    extension, and each irrelevant hit is noise injected into the context that
    retrieval exists to keep small.
    """
    tokens = _tokens(query)
    if not tokens:
        return []
    phrase = '"' + " ".join(tokens) + '"'
    if len(tokens) == 1:
        return [phrase]
    return [phrase, " OR ".join(f'"{t}"' for t in tokens)]


class SqliteMemoryStore(MemoryStore):
    """FTS5-backed store. Zero dependencies, zero spend, deterministic."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Clock | None = None,
        ids: IdSource | None = None,
    ) -> None:
        self._path = Path(path)
        self._clock = clock or SystemClock()
        self._ids = ids or UuidIds()
        if str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteMemoryStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                item_id    TEXT PRIMARY KEY,
                task_id    TEXT,
                kind       TEXT NOT NULL,
                text       TEXT NOT NULL,
                tags_json  TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_items_task ON items(task_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS items_fts
                USING fts5(body, content='');
            """
        )
        self._conn.commit()

    # -- writing -----------------------------------------------------------

    def new_item(
        self,
        text: str,
        *,
        task_id: str | None = None,
        kind: MemoryKind = MemoryKind.PRUNED,
        tags: Sequence[str] = (),
    ) -> MemoryItem:
        return MemoryItem(
            item_id=self._ids.new_id("mem"),
            task_id=task_id,
            kind=kind,
            text=text,
            tags=list(tags),
            created_at=self._clock.now(),
        )

    async def write(self, *items: MemoryItem) -> int:
        for item in items:
            cur = self._conn.execute(
                """
                INSERT INTO items (item_id, task_id, kind, text, tags_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO NOTHING
                """,
                (
                    item.item_id,
                    item.task_id,
                    item.kind.value,
                    item.text,
                    json.dumps(item.tags),
                    item.created_at.isoformat(),
                ),
            )
            if cur.rowcount:
                # rowid ties the contentless FTS row to the items row.
                self._conn.execute(
                    "INSERT INTO items_fts (rowid, body) VALUES ((SELECT rowid FROM items WHERE item_id = ?), ?)",
                    (item.item_id, item.searchable()),
                )
        self._conn.commit()
        return len(items)

    # -- reading -----------------------------------------------------------

    def _row_to_item(self, row: sqlite3.Row) -> MemoryItem:
        return MemoryItem(
            item_id=row["item_id"],
            task_id=row["task_id"],
            kind=MemoryKind(row["kind"]),
            text=row["text"],
            tags=json.loads(row["tags_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def get(self, item_id: str) -> MemoryItem | None:
        row = self._conn.execute(
            "SELECT * FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        return self._row_to_item(row) if row else None

    async def search(
        self, query: str, *, limit: int = 10, task_id: str | None = None
    ) -> list[MemoryItem]:
        """Best matches first, by BM25 rank.

        An empty or punctuation-only query returns nothing rather than
        everything: a retrieval step that silently matched the whole store would
        flood the context it was meant to keep small.
        """
        for match in _match_queries(query):
            sql = """
                SELECT items.* FROM items_fts
                  JOIN items ON items.rowid = items_fts.rowid
                 WHERE items_fts MATCH ?
            """
            params: list[object] = [match]
            if task_id is not None:
                sql += " AND items.task_id = ?"
                params.append(task_id)
            sql += " ORDER BY bm25(items_fts), items.created_at DESC LIMIT ?"
            params.append(limit)

            rows = self._conn.execute(sql, params).fetchall()
            if rows:
                return [self._row_to_item(r) for r in rows]
        return []

    async def recent(
        self, *, limit: int = 10, task_id: str | None = None
    ) -> list[MemoryItem]:
        sql = "SELECT * FROM items"
        params: list[object] = []
        if task_id is not None:
            sql += " WHERE task_id = ?"
            params.append(task_id)
        sql += " ORDER BY created_at DESC, item_id DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_item(r) for r in rows]

    async def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])


class InMemoryStore(MemoryStore):
    """Non-durable store for tests that only need the interface."""

    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}

    async def write(self, *items: MemoryItem) -> int:
        for item in items:
            self._items.setdefault(item.item_id, item)
        return len(items)

    async def search(
        self, query: str, *, limit: int = 10, task_id: str | None = None
    ) -> list[MemoryItem]:
        """Phrase first, then any token — the same precedence as the FTS5 store,
        so swapping implementations does not change retrieval behaviour."""
        tokens = [t.lower() for t in _tokens(query)]
        if not tokens:
            return []

        scoped = [
            item
            for item in self._items.values()
            if task_id is None or item.task_id == task_id
        ]
        phrase = " ".join(tokens)
        exact = [
            i for i in scoped if phrase in " ".join(_tokens(i.searchable())).lower()
        ]
        chosen = exact or [
            i
            for i in scoped
            if set(tokens) & {t.lower() for t in _tokens(i.searchable())}
        ]
        return sorted(chosen, key=lambda i: i.created_at, reverse=True)[:limit]

    async def get(self, item_id: str) -> MemoryItem | None:
        return self._items.get(item_id)

    async def count(self) -> int:
        return len(self._items)

    def all(self) -> Iterable[MemoryItem]:
        return list(self._items.values())
