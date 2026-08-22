"""The execution plane: dispatch, tools, and the escalation ladder."""

from aop.execution.ladder import EscalationLadder, LadderResult, LadderStep
from aop.execution.plane import ExecutionPlane, PlaneOutcome
# ClaudeCodePlane is deliberately NOT re-exported here: importing it pulls the
# optional Agent SDK seam into every `from aop.execution import ...`, and the
# internal plane must keep working with the SDK absent. Import it by module.
from aop.execution.tools import FileTools, ShellTools, ToolSurface, build_toolbox, describe
from aop.execution.worker import Worker, WorkerResult, render_spec

__all__ = [
    "EscalationLadder",
    "ExecutionPlane",
    "FileTools",
    "LadderResult",
    "LadderStep",
    "PlaneOutcome",
    "ShellTools",
    "ToolSurface",
    "Worker",
    "WorkerResult",
    "build_toolbox",
    "describe",
    "render_spec",
]
