"""
The record of a handoff.

One file per intervention under evidence/<run_id>/interventions/<id>.json,
written when the request opens and rewritten when the operator hands control
back. An intervention that is still open is one whose file has no outcome: the
run folder alone tells you what happened, without the console being up.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return f"int_{uuid.uuid4().hex[:8]}"


class InterventionReason(str, Enum):
    TARGET_UNRESOLVED = "target_unresolved"
    CHECKPOINT_MISMATCH = "checkpoint_mismatch"
    UNKNOWN_DIALOG = "unknown_dialog"
    RISKY_ACTION_CONFIRMATION = "risky_action_confirmation"
    NO_PROGRESS = "no_progress"


class InterventionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=new_id)
    run_id: str
    capability_ref: str
    step_id: str | None = None
    step_index: int | None = None
    reason: InterventionReason
    expected: str = ""
    observed: str = ""
    screenshot_path: str | None = None
    ax_snapshot_path: str | None = None
    created_at: str = Field(default_factory=now_iso)

    @property
    def title(self) -> str:
        where = self.step_id or "before the flow"
        return f"{self.reason.value} at {where}"


class InterventionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved: bool
    operator_notes: str = ""
    ended_at: str = Field(default_factory=now_iso)


def folder_for(run_id: str, root: Path = Path("evidence")) -> Path:
    return root / run_id / "interventions"


def path_for(request: InterventionRequest, root: Path = Path("evidence")) -> Path:
    return folder_for(request.run_id, root) / f"{request.id}.json"


def record_request(request: InterventionRequest, root: Path = Path("evidence")) -> Path:
    """Written the moment the operator is asked for, not when they answer - a
    run that dies mid-handoff still leaves the reason behind."""
    path = path_for(request, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"request": request.model_dump(mode="json"), "outcome": None}, indent=2),
        encoding="utf-8",
    )
    return path


def record_outcome(
    request: InterventionRequest,
    outcome: InterventionOutcome,
    root: Path = Path("evidence"),
) -> Path:
    path = path_for(request, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "request": request.model_dump(mode="json"),
                "outcome": outcome.model_dump(mode="json"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def load(path: Path) -> tuple[InterventionRequest, InterventionOutcome | None]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    outcome = raw.get("outcome")
    return (
        InterventionRequest.model_validate(raw["request"]),
        InterventionOutcome.model_validate(outcome) if outcome else None,
    )
