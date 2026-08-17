"""The conductor: the only thing that talks to the user, and the main cost dial.

Everything here is about spending its thinking sparingly — checkpoints rather
than continuous attention, low effort by default, and structured output rather
than prose it could reword.
"""

from aop.conductor.authorship import (
    AuthoredTests,
    author_acceptance_tests,
    default_test_path,
    freeze_existing,
)
from aop.conductor.checkpoints import (
    WAKES_CONDUCTOR,
    Checkpoint,
    CheckpointLog,
    CheckpointRecord,
    NotACheckpoint,
    checkpoint_for,
    effort_for,
)
from aop.conductor.directive import Directive, DirectiveGuard, DirectiveViolation
from aop.conductor.rationale import PlanCheck, Rationale, check_plan, record_rationale
from aop.conductor.taskspec import (
    SpecEmission,
    SpecEmissionFailed,
    emit_spec,
    extract_json,
    parse_spec,
    spec_schema,
)

__all__ = [
    "WAKES_CONDUCTOR",
    "AuthoredTests",
    "Checkpoint",
    "CheckpointLog",
    "CheckpointRecord",
    "Directive",
    "DirectiveGuard",
    "DirectiveViolation",
    "NotACheckpoint",
    "PlanCheck",
    "Rationale",
    "SpecEmission",
    "SpecEmissionFailed",
    "author_acceptance_tests",
    "check_plan",
    "checkpoint_for",
    "default_test_path",
    "effort_for",
    "emit_spec",
    "extract_json",
    "freeze_existing",
    "parse_spec",
    "record_rationale",
    "spec_schema",
]
