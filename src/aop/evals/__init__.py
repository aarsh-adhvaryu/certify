"""Evaluation: a saved suite of representative tasks, graded by the verifier gate.

Spec §3.1's "swap and auto-validate". Originally the last slot in the plan; moved
near the front because it is the instrument that decides which model fills which
role, and arguing from benchmark tables is not measurement.
"""

from aop.evals.harness import Comparison, Harness, RunReport, TaskResult, compare
from aop.evals.suite import EvalSuite, EvalTask, SuiteError

__all__ = [
    "Comparison",
    "EvalSuite",
    "EvalTask",
    "Harness",
    "RunReport",
    "SuiteError",
    "TaskResult",
    "compare",
]
