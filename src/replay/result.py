"""
What a replay hands back.

Three arms, because the caller has to treat them differently:

  Success         - the capability did what it promises, outputs are populated.
  BusinessOutcome - the app gave a legitimate answer that isn't the happy path
                    ("no such member"). Declared up front in the artifact, so a
                    calling agent can branch on `code` instead of scraping text.
  Failure         - we could not complete the flow. Always carries a
                    failure_class the caller can route on, plus the expected /
                    observed pair a human needs to triage it.

Every arm carries the same step_log and evidence_ref: the run is auditable
whether it worked or not.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class FailureClass(str, Enum):
    TIMEOUT = "timeout"
    CHECKPOINT_MISMATCH = "checkpoint_mismatch"
    TARGET_UNRESOLVED = "target_unresolved"
    GUARDRAIL_BLOCK = "guardrail_block"
    ESCALATION_UNRESOLVED = "escalation_unresolved"
    SURFACE_ERROR = "surface_error"
    INVALID_INPUT = "invalid_input"


class StepLog(BaseModel):
    """One flow step as executed. `resolved_tier` > 0 is the drift signal: the
    primary binding stopped working and a fallback carried the step."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    intent: str
    resolved_strategy: str | None = None
    resolved_tier: int | None = None
    duration_ms: int = 0
    recoveries: list[str] = Field(default_factory=list)
    checkpoint_ok: bool | None = None
    note: str | None = None


class _ResultBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_ref: str
    step_log: list[StepLog] = Field(default_factory=list)

    def to_jsonl_lines(self) -> list[str]:
        """The step log as JSONL, one entry per line. This is what lands in
        evidence/<run_id>/replay.jsonl."""
        return [
            json.dumps(entry.model_dump(mode="json"), separators=(",", ":"))
            for entry in self.step_log
        ]


class Success(_ResultBase):
    kind: Literal["success"] = "success"
    outputs: dict[str, Any] = Field(default_factory=dict)


class BusinessOutcome(_ResultBase):
    """Not an error. The flow stopped early because the app answered."""

    kind: Literal["business_outcome"] = "business_outcome"
    code: str
    message: str
    outputs: dict[str, Any] = Field(default_factory=dict)


class Failure(_ResultBase):
    kind: Literal["failure"] = "failure"
    step_id: str | None = None
    step_index: int | None = None
    failure_class: FailureClass
    expected: str
    observed: str


ReplayResult = Annotated[
    Union[Success, BusinessOutcome, Failure],
    Field(discriminator="kind"),
]
