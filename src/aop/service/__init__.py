"""The local service: a headless orchestrator with a WebSocket event stream.

The UI is a *subscriber*, not a component. That separation is what lets the
overlay crash, be restarted, or be replaced by a plain browser tab without
disturbing a task that is mid-flight — and it is why the frameless-window work
later carries no risk of blocking anything.
"""

from aop.service.app import build_app, serve

__all__ = ["build_app", "serve"]
