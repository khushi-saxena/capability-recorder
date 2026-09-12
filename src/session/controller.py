"""
The automation/operator handoff.

One browser session, two possible drivers, never both. The lease says which one
is allowed to act; `require` turns a race into an exception at the call site
instead of a click that lands in someone else's screen.

Threading note that shapes this file: the Playwright session belongs to the
thread that created it, which is the thread running replay(). The console runs
in another thread and therefore cannot touch the surface itself. While
`escalate` blocks waiting for the operator, it pumps frame requests on behalf of
the console - the waiting thread is the only one allowed to take the picture.
"""

from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path
from typing import Any, Iterable

from src.session.intervention import (
    InterventionOutcome,
    InterventionReason,
    InterventionRequest,
    now_iso,
    record_outcome,
    record_request,
)
from src.surface.base import ax_snapshot

AUTOMATION = "automation"
OPERATOR = "operator"

PUMP_INTERVAL_S = 0.2
FRAME_TIMEOUT_S = 8.0

# The executor names escalations in its own terms; these are the same events.
_REASON_BY_KIND = {
    "target_unresolved": InterventionReason.TARGET_UNRESOLVED,
    "checkpoint_mismatch": InterventionReason.CHECKPOINT_MISMATCH,
    "risky_action_confirmation": InterventionReason.RISKY_ACTION_CONFIRMATION,
    "guardrail_confirmation": InterventionReason.RISKY_ACTION_CONFIRMATION,
    "no_progress": InterventionReason.NO_PROGRESS,
}


class LeaseViolation(RuntimeError):
    """Raised when a driver acts without holding the lease."""


class SessionController:
    """Wraps a live surface and arbitrates who may drive it."""

    def __init__(self, surface, policy=None, evidence_root: Path | str = "evidence"):
        self.surface = surface
        self.policy = policy
        self.evidence_root = Path(evidence_root)

        self.lease: str = AUTOMATION
        self.holder_since: str = now_iso()
        self.current_request: InterventionRequest | None = None

        self._lock = threading.RLock()
        self._returned = threading.Event()
        self._outcome: InterventionOutcome | None = None
        self._operator_taken = False
        self._requests: dict[str, InterventionRequest] = {}
        self._outcomes: dict[str, InterventionOutcome] = {}
        self._taken_at: dict[str, str] = {}

        # console -> executor thread, for live screenshots
        self._frame_wanted = threading.Event()
        self._frame_ready = threading.Event()
        self._frame_path: str | None = None

    # -- lease -------------------------------------------------------------

    def require(self, holder: str) -> None:
        """Called before every action. Cheap, and the only thing standing
        between a slow automation step and the operator's own clicks."""
        if self.lease != holder:
            raise LeaseViolation(
                f"{holder} tried to act while the {self.lease} lease is held"
                + (f" (intervention {self.current_request.id})" if self.current_request else "")
            )

    def take_control(self, request_id: str | None = None) -> dict:
        """The operator asserting the lease from the console. The browser is
        headed, so from here they just drive the real window."""
        with self._lock:
            if self.current_request is None:
                raise LeaseViolation("no intervention is open; automation still holds the lease")
            if request_id is not None and request_id != self.current_request.id:
                raise LeaseViolation(f"intervention {request_id} is not the open one")
            self.lease = OPERATOR
            self.holder_since = now_iso()
            self._operator_taken = True
            self._taken_at[self.current_request.id] = self.holder_since
            return self.status()

    def return_to_automation(self, resolved: bool, notes: str = "") -> InterventionOutcome:
        """Records the outcome and unblocks the waiting executor. It does not
        decide whether the run continues - the executor re-checks the step."""
        with self._lock:
            request = self.current_request
            if request is None:
                raise LeaseViolation("no intervention is open")
            if self.lease != OPERATOR or not self._operator_taken:
                raise LeaseViolation("operator must take control before returning control")
            outcome = InterventionOutcome(resolved=resolved, operator_notes=notes)
            record_outcome(request, outcome, self.evidence_root)
            self._outcomes[request.id] = outcome
            self._outcome = outcome
            self.lease = AUTOMATION
            self.holder_since = now_iso()
            self._returned.set()
            return outcome

    # -- escalation --------------------------------------------------------

    def escalate(self, request: Any) -> bool:
        """Satisfies the executor's Escalator protocol. Blocks the automation
        thread until a human hands the session back."""
        intervention = self._open(request)
        try:
            while not self._returned.wait(PUMP_INTERVAL_S):
                self._serve_frame_request()
            self._serve_frame_request()
            outcome = self._outcome or InterventionOutcome(
                resolved=False, operator_notes="control returned with no outcome recorded"
            )
            return outcome.resolved
        finally:
            with self._lock:
                self._outcome = None
                self._returned.clear()
                if self.current_request is intervention:
                    self.current_request = None
                self._operator_taken = False
                self.lease = AUTOMATION
                self.holder_since = now_iso()

    def _open(self, request: Any) -> InterventionRequest:
        intervention = self._as_intervention(request)
        with self._lock:
            record_request(intervention, self.evidence_root)
            self._requests[intervention.id] = intervention
            self.current_request = intervention
            self._outcome = None
            self._returned.clear()
            self._operator_taken = False
            # The operator owns the session from the moment we ask, whether or
            # not they have pressed Take control yet.
            self.lease = OPERATOR
            self.holder_since = now_iso()
        return intervention

    def _as_intervention(self, request: Any) -> InterventionRequest:
        if isinstance(request, InterventionRequest):
            return request
        reason = _REASON_BY_KIND.get(getattr(request, "kind", ""), InterventionReason.UNKNOWN_DIALOG)
        run_id = getattr(request, "run_id", "adhoc")
        shot, snapshot = self._capture(run_id)
        return InterventionRequest(
            run_id=run_id,
            capability_ref=getattr(request, "capability", "unknown"),
            step_id=getattr(request, "step_id", None),
            step_index=getattr(request, "step_index", None),
            reason=reason,
            expected=getattr(request, "expected", "") or getattr(request, "reason", ""),
            observed=getattr(request, "observed", ""),
            screenshot_path=shot,
            ax_snapshot_path=snapshot,
        )

    # -- evidence ----------------------------------------------------------

    def _capture(self, run_id: str) -> tuple[str | None, str | None]:
        """Best effort: a request with no picture is still worth raising."""
        shot = snapshot = None
        try:
            obs = self.surface.observe()
        except Exception:
            return None, None
        try:
            shot = self._screenshot(run_id, self._sensitive(obs))
        except Exception:
            shot = None
        try:
            folder = self.evidence_root / run_id / "interventions"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"ax_{len(self._requests):02d}.json"
            redact = self.policy.redact if self.policy else None
            path.write_text(json.dumps(ax_snapshot(obs, redact), indent=2), encoding="utf-8")
            snapshot = str(path)
        except Exception:
            snapshot = None
        return shot, snapshot

    def _sensitive(self, obs) -> list[int]:
        return self.policy.sensitive_handles(obs) if self.policy else []

    def _screenshot(self, run_id: str, mask_handles: Iterable[int]) -> str:
        kwargs = {}
        try:
            if "run_id" in inspect.signature(self.surface.screenshot).parameters:
                kwargs["run_id"] = run_id
        except (TypeError, ValueError):
            pass
        return self.surface.screenshot(list(mask_handles), **kwargs)

    # -- live frames for the console ---------------------------------------

    def request_frame(self, timeout: float = FRAME_TIMEOUT_S) -> str | None:
        """Console thread: ask the executor thread for a fresh screenshot.
        Returns None if nobody is waiting to serve it."""
        with self._lock:
            self._frame_ready.clear()
            self._frame_wanted.set()
        if not self._frame_ready.wait(timeout):
            self._frame_wanted.clear()
            return None
        return self._frame_path

    def _serve_frame_request(self) -> None:
        if not self._frame_wanted.is_set():
            return
        self._frame_wanted.clear()
        previous = self._frame_path
        try:
            run_id = self.current_request.run_id if self.current_request else "adhoc"
            self._frame_path = self._screenshot(f"{run_id}/frames", self._sensitive(self.surface.observe()))
        except Exception:
            self._frame_path = None
        # Keep one live frame, not a flipbook of every poll.
        if previous and previous != self._frame_path:
            Path(previous).unlink(missing_ok=True)
        self._frame_ready.set()

    # -- console views -----------------------------------------------------

    def status(self) -> dict:
        request = self.current_request
        return {
            "lease": self.lease,
            "holder_since": self.holder_since,
            "current_request": request.model_dump(mode="json") if request else None,
            "taken_at": self._taken_at.get(request.id) if request else None,
            "open": [r.id for r in self.open_requests()],
        }

    def open_requests(self) -> list[InterventionRequest]:
        return [r for rid, r in self._requests.items() if rid not in self._outcomes]

    def all_requests(self) -> list[tuple[InterventionRequest, InterventionOutcome | None]]:
        return [(r, self._outcomes.get(rid)) for rid, r in self._requests.items()]

    def get(self, request_id: str) -> tuple[InterventionRequest | None, InterventionOutcome | None]:
        return self._requests.get(request_id), self._outcomes.get(request_id)
