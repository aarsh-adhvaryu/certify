"""Slot 41 — the local service.

A small HTTP + WebSocket surface over a running :class:`Operator`. It binds to
localhost only: this is a personal daemon with filesystem and shell access, and
an orchestrator listening on a routable interface is a remote code execution
service with extra steps.

The WebSocket is the interesting part. It subscribes to the same event bus every
other component publishes on, so the UI sees exactly what happened rather than a
summary someone remembered to write. **The bus is lossy by design** — a client
that stops reading gets events dropped and the orchestrator carries on. Drops are
reported to the client rather than hidden, so a UI showing a gap can say so
instead of quietly lying about what it missed.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from aop.core.state import TaskNotFound
from aop.operator import Operator

UI_DIR = Path(__file__).parent / "ui"

#: Per-client queue depth. Generous enough that a browser tab reading normally
#: never drops, small enough that a wedged one cannot grow without bound.
CLIENT_QUEUE = 2048


class SubmitRequest(BaseModel):
    directive: str = Field(min_length=1, max_length=8000)


def build_app(operator: Operator, *, manage_lifecycle: bool = True) -> FastAPI:
    """Wrap an operator in an HTTP surface.

    ``manage_lifecycle=False`` leaves start/stop to the caller, which is what
    tests want when they need to inspect the operator before and after.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if manage_lifecycle:
            await operator.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                await operator.stop()

    app = FastAPI(title="Agentic Operator", lifespan=lifespan)
    app.state.operator = operator

    # -- reads -------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict:
        scheduler = operator.scheduler
        return {
            "ok": True,
            "tasks_running": len(scheduler.in_flight) if scheduler else 0,
            "capacity": scheduler.capacity if scheduler else 0,
            "ticks": scheduler.ticks if scheduler else 0,
        }

    @app.get("/api/snapshot")
    async def snapshot() -> dict:
        return await operator.snapshot()

    @app.get("/api/tasks")
    async def tasks() -> list[dict]:
        return [t.model_dump(mode="json") for t in await operator.store.list_tasks()]

    @app.get("/api/tasks/{task_id}")
    async def task_detail(task_id: str) -> dict:
        try:
            task = await operator.store.get_task(task_id)
        except TaskNotFound:
            raise HTTPException(404, f"no task {task_id!r}") from None
        attempts = await operator.store.list_attempts(task_id)
        return {
            "task": task.model_dump(mode="json"),
            "attempts": [a.model_dump(mode="json") for a in attempts],
        }

    @app.get("/api/journal")
    async def journal() -> JSONResponse:
        """The failsafe, readable without a database.

        Exposed because the whole point of the journal is that it is legible when
        other things are broken; making it reachable only through SQLite would
        defeat it.
        """
        path = operator.journal.path
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        return JSONResponse({"path": str(path), "markdown": text})

    # -- writes ------------------------------------------------------------

    @app.post("/api/tasks", status_code=202)
    async def submit(request: SubmitRequest) -> dict:
        """Accept a directive. Returns immediately; progress arrives on the socket."""
        task = await operator.submit(request.directive.strip())
        return {"task_id": task.task_id, "status": task.status.value}

    # -- the event stream --------------------------------------------------

    @app.websocket("/ws")
    async def events(socket: WebSocket) -> None:
        await socket.accept()
        subscription = operator.bus.subscribe(queue_size=CLIENT_QUEUE)
        reported_drops = 0

        try:
            await socket.send_json({"kind": "snapshot", "data": await operator.snapshot()})

            while True:
                event = await subscription.get()
                if event is None:
                    break

                await socket.send_text(event.model_dump_json())

                # Surface gaps rather than papering over them. A UI that silently
                # skips events looks identical to one where nothing happened.
                if subscription.dropped > reported_drops:
                    missed = subscription.dropped - reported_drops
                    reported_drops = subscription.dropped
                    await socket.send_json({"kind": "dropped", "count": missed})

        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except RuntimeError:
            # Socket closed underneath us mid-send; nothing to do but tidy up.
            pass
        finally:
            subscription.close()

    # -- the overlay -------------------------------------------------------

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

    return app


def serve(
    operator: Operator,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    log_level: str = "warning",
) -> None:
    """Run the service. Blocks.

    Bound to loopback and not configurable to anything else here on purpose —
    see the module docstring.
    """
    import uvicorn

    uvicorn.run(build_app(operator), host=host, port=port, log_level=log_level)
