"""
Deterministic replay of a capability artifact.

No model in the loop. The artifact says what to do, the surface does it, the
policy says whether it may, and everything that happens lands in the step log.

The per-step order is fixed and matters:

    observe -> recover -> expected outcome -> bind+resolve -> policy -> act
    -> checkpoint

Recovery runs before outcome detection so a compliance interstitial never gets
mistaken for a business answer. Outcome detection runs before the step acts, so
"no such member" stops the flow instead of blindly clicking into a detail page
that isn't there. Policy runs after resolution and before the act, which is the
only point where we know both what we're about to touch and where we are.
"""

from __future__ import annotations

import inspect
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

from src.policy.policy import Allow, Deny, Policy, RequireConfirmation
from src.replay.predicates import describe, evaluate
from src.replay.result import (
    BusinessOutcome,
    Failure,
    FailureClass,
    ReplayResult,
    StepLog,
    Success,
)
from src.schema import (
    ActivateStep,
    Capability,
    CoordinateCandidate,
    ExpectedOutcome,
    FillStep,
    NavigateStep,
    ParseAs,
    ReadStep,
    RecoverableCondition,
    RecoveryAction,
    SelectStep,
    Step,
    Target,
)
from src.surface.base import (
    Observation,
    Resolution,
    Surface,
    TargetUnresolved,
    bind_templates,
    resolve,
)

EVIDENCE_DIR = Path("evidence")
CHECKPOINT_POLL_S = 0.2
SETTLE_S = 0.5
OBSERVED_CHARS = 280


# --------------------------------------------------------------------------
# Escalation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EscalationRequest:
    """Handed to a human (or, later, an operator agent) when replay is stuck.
    Carries enough to act on without reading the artifact."""

    kind: str  # target_unresolved | guardrail_confirmation | checkpoint_mismatch
    run_id: str
    capability: str
    reason: str
    step_id: str | None = None
    step_index: int | None = None
    expected: str = ""
    observed: str = ""


class Escalator(Protocol):
    """True means resolved - the executor retries and carries on. False means
    it stays stuck and the run fails with the class of the thing that blocked."""

    def escalate(self, request: EscalationRequest) -> bool: ...


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def replay(
    capability: Capability,
    params: dict[str, Any],
    surface: Surface,
    policy: Policy,
    escalator: Escalator | None = None,
    run_id: str | None = None,
) -> ReplayResult:
    """Run one capability against one surface. Never raises for a business or
    operational problem - those come back as BusinessOutcome or Failure."""
    return _Replay(capability, params, surface, policy, escalator, run_id).run()


# --------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------


class _Abort(Exception):
    """Internal control flow. Carries everything a Failure needs; the runner
    turns it into one, after capturing evidence."""

    def __init__(
        self,
        failure_class: FailureClass,
        expected: str,
        observed: str,
        step_id: str | None = None,
        step_index: int | None = None,
        capture: bool = True,
    ):
        super().__init__(f"{failure_class.value}: {expected} != {observed}")
        self.failure_class = failure_class
        self.expected = expected
        self.observed = observed
        self.step_id = step_id
        self.step_index = step_index
        self.capture = capture  # False when the browser was never touched


@dataclass
class _StepState:
    started: float
    recoveries: list[str] = field(default_factory=list)
    strategy: str | None = None
    tier: int | None = None
    checkpoint_ok: bool | None = None
    note: str | None = None


class _Replay:
    def __init__(
        self,
        capability: Capability,
        params: dict[str, Any],
        surface: Surface,
        policy: Policy,
        escalator: Escalator | None,
        run_id: str | None,
    ):
        self.cap = capability
        self.params = {k: "" if v is None else str(v) for k, v in (params or {}).items()}
        self.surface = surface
        self.policy = policy
        self.escalator = escalator
        self.run_id = run_id or f"replay_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
        self.evidence_dir = EVIDENCE_DIR / self.run_id

        self.step_log: list[StepLog] = []
        self.executed: list[str] = []
        self.reads: dict[str, tuple[str, ParseAs]] = {}

    # -- run ---------------------------------------------------------------

    def run(self) -> ReplayResult:
        try:
            self._validate_params()
            outcome = self._run_flow()
            result: ReplayResult = outcome if outcome is not None else self._final_result()
        except _Abort as abort:
            result = self._failure(abort)
        return self._write_evidence(result)

    def _run_flow(self) -> BusinessOutcome | None:
        for index, step in enumerate(self.cap.flow):
            state = _StepState(started=time.monotonic())
            try:
                outcome = self._run_step(step, index, state)
            except _Abort:
                self._log(step, state)
                raise
            self._log(step, state)
            if outcome is not None:
                # Built after the log entry so the result carries it.
                return self._business_outcome(outcome)
            self.executed.append(step.id)
        return None

    def _run_step(self, step: Step, index: int, state: _StepState) -> ExpectedOutcome | None:
        obs = self._observe(step, index)
        obs = self._recover(obs, state, step, index)

        outcome = self._expected_outcome(obs)
        if outcome is not None:
            state.note = f"not executed: business outcome {outcome.code}"
            return outcome

        if isinstance(step, NavigateStep):
            url = _bind_text(step.url_template, self.params)
            self._check(self.policy.check_navigate(url), step, index, f"navigate {url}")
            self._act(step, index, "navigate", lambda: self.surface.navigate(url))
            state.strategy = "url"
        else:
            target = bind_templates(step.target, self.params)
            resolution, coordinate, obs = self._resolve(step, index, target, obs)
            state.strategy = resolution.strategy if resolution else "coordinate"
            state.tier = resolution.tier if resolution else _coordinate_tier(target)

            self._check(
                self.policy.check_action(step.intent, obs.url, step.risk.value),
                step,
                index,
                f"{step.intent} on {step.target.describes}",
            )
            self._perform(step, index, resolution, coordinate)

        if step.checkpoint is not None:
            state.checkpoint_ok, obs = self._poll_checkpoint(step, index)

            # A checkpoint that fails because the app gave a declared answer is
            # that answer, not a failure: "no such member" lands on the search
            # checkpoint, one step before the outcome's after_step goes live.
            if not state.checkpoint_ok:
                outcome = self._expected_outcome(
                    obs, also_executed=step.id, affirmative_only=True
                )
                if outcome is not None:
                    state.note = f"checkpoint superseded by business outcome {outcome.code}"
                    return outcome

            if not state.checkpoint_ok and self._escalate(
                "checkpoint_mismatch",
                step,
                index,
                f"checkpoint for step {step.id} did not hold within {step.timeout_ms}ms",
                describe(step.checkpoint),
                self._slice(obs, step.checkpoint),
            ):
                state.checkpoint_ok, obs = self._poll_checkpoint(step, index)
            if not state.checkpoint_ok:
                raise _Abort(
                    FailureClass.CHECKPOINT_MISMATCH,
                    describe(step.checkpoint),
                    self._slice(obs, step.checkpoint),
                    step.id,
                    index,
                )
        return None

    def _final_result(self) -> ReplayResult:
        obs = self._observe(None, None)
        if not evaluate(self.cap.success, obs, self.surface):
            raise _Abort(
                FailureClass.CHECKPOINT_MISMATCH,
                describe(self.cap.success),
                self._slice(obs, self.cap.success),
            )
        return Success(
            outputs=self._collect_outputs(),
            evidence_ref=str(self.evidence_dir),
            step_log=self.step_log,
        )

    # -- inputs ------------------------------------------------------------

    def _validate_params(self) -> None:
        """Contract check, before the browser is touched at all. A bad call
        should cost nothing and should never half-run a mutating flow."""
        problems: list[str] = []
        for name, spec in self.cap.inputs.items():
            value = self.params.get(name, "")
            if not value:
                if spec.required:
                    problems.append(f"missing required input {name!r}")
                continue
            # Author-supplied patterns carry their own anchors.
            if spec.pattern and re.search(spec.pattern, value) is None:
                problems.append(f"input {name!r} does not match {spec.pattern!r}")
        if problems:
            raise _Abort(
                FailureClass.INVALID_INPUT,
                f"inputs satisfying {self.cap.ref}",
                "; ".join(problems),
                capture=False,
            )

    # -- perception and recovery -------------------------------------------

    def _observe(self, step: Step | None, index: int | None) -> Observation:
        try:
            return self.surface.observe()
        except Exception as exc:
            raise _Abort(
                FailureClass.SURFACE_ERROR,
                "an observable surface",
                _short(exc),
                step.id if step else None,
                index,
            ) from exc

    def _recover(
        self, obs: Observation, state: _StepState, step: Step, index: int
    ) -> Observation:
        """Handle known-annoying conditions in place. Bounded by each
        condition's max_attempts, so this terminates. Never reaches the caller -
        the only trace is the step log."""
        attempts: dict[str, int] = {}
        while True:
            condition = next(
                (
                    c
                    for c in self.cap.recoverable
                    if attempts.get(c.name, 0) < c.max_attempts
                    and evaluate(c.detect, obs, self.surface)
                ),
                None,
            )
            if condition is None:
                return obs
            attempts[condition.name] = attempts.get(condition.name, 0) + 1
            state.recoveries.append(self._apply_recovery(condition, obs))
            obs = self._observe(step, index)

    def _apply_recovery(self, condition: RecoverableCondition, obs: Observation) -> str:
        if condition.action is RecoveryAction.DISMISS:
            target = bind_templates(condition.dismiss_target, self.params)
            try:
                self.surface.activate(resolve(target, obs).handle)
            except Exception:  # includes TargetUnresolved; recovery is best effort
                return f"{condition.name}:dismiss_failed"
            return f"{condition.name}:dismiss"

        if condition.action is RecoveryAction.RELOAD:
            try:
                self.surface.navigate(obs.url)
            except Exception:
                return f"{condition.name}:reload_failed"
            return f"{condition.name}:reload"

        time.sleep(SETTLE_S)  # retry_step: re-observe and take the step again
        return f"{condition.name}:retry_step"

    def _expected_outcome(
        self,
        obs: Observation,
        also_executed: str | None = None,
        affirmative_only: bool = False,
    ) -> ExpectedOutcome | None:
        """Only outcomes whose gating step has already run. An outcome with no
        after_step is live from the start.

        affirmative_only drops negated detects. Absence of text is true of any
        broken page, so it may not be used to reinterpret a failed checkpoint as
        a business answer - that would hide real breakage behind a clean code."""
        executed = set(self.executed) | ({also_executed} if also_executed else set())
        for outcome in self.cap.expected_outcomes:
            if outcome.after_step is not None and outcome.after_step not in executed:
                continue
            if affirmative_only and outcome.detect.negate:
                continue
            if evaluate(outcome.detect, obs, self.surface):
                return outcome
        return None

    def _business_outcome(self, outcome: ExpectedOutcome) -> BusinessOutcome:
        return BusinessOutcome(
            code=outcome.code,
            message=outcome.describes,
            outputs=self._collect_outputs(),
            evidence_ref=str(self.evidence_dir),
            step_log=self.step_log,
        )

    # -- resolution, policy, action ----------------------------------------

    def _resolve(
        self, step: Step, index: int, target: Target, obs: Observation
    ) -> tuple[Resolution | None, CoordinateCandidate | None, Observation]:
        try:
            return resolve(target, obs), None, obs
        except TargetUnresolved as unresolved:
            if self._escalate(
                "target_unresolved",
                step,
                index,
                f"could not find {target.describes!r}",
                target.describes,
                f"tried {unresolved.tried}",
            ):
                obs = self._observe(step, index)
                try:
                    return resolve(target, obs), None, obs
                except TargetUnresolved:
                    pass

            # Coordinates never produce a handle; the executor clicks them
            # directly. Last resort, and only for activation.
            coordinate = _coordinate_candidate(target)
            if coordinate is not None and isinstance(step, ActivateStep):
                return None, coordinate, obs

            raise _Abort(
                FailureClass.TARGET_UNRESOLVED,
                target.describes,
                f"no candidate matched; tried {unresolved.tried}",
                step.id,
                index,
            ) from unresolved

    def _check(self, decision, step: Step, index: int, what: str) -> None:
        if isinstance(decision, Allow):
            return
        if isinstance(decision, Deny):
            raise _Abort(
                FailureClass.GUARDRAIL_BLOCK,
                f"policy allows {what}",
                decision.reason,
                step.id,
                index,
            )
        if isinstance(decision, RequireConfirmation):
            if self._escalate(
                "guardrail_confirmation",
                step,
                index,
                decision.reason,
                f"confirmation for {what}",
                decision.reason,
            ):
                return
            raise _Abort(
                FailureClass.GUARDRAIL_BLOCK,
                f"confirmation for {what}",
                f"unconfirmed: {decision.reason}",
                step.id,
                index,
            )
        raise TypeError(f"unknown policy decision: {decision!r}")

    def _perform(
        self,
        step: Step,
        index: int,
        resolution: Resolution | None,
        coordinate: CoordinateCandidate | None,
    ) -> None:
        if coordinate is not None:
            self._act(
                step,
                index,
                step.intent,
                lambda: self.surface.activate_at(coordinate.x_ratio, coordinate.y_ratio),
            )
            return

        handle = resolution.handle
        if isinstance(step, FillStep):
            value = _bind_text(step.value_template, self.params)
            self._act(step, index, "fill", lambda: self.surface.fill(handle, value))
        elif isinstance(step, SelectStep):
            option = _bind_text(step.option_template, self.params)
            self._act(step, index, "select", lambda: self.surface.select(handle, option))
        elif isinstance(step, ActivateStep):
            self._act(step, index, "activate", lambda: self.surface.activate(handle))
        elif isinstance(step, ReadStep):
            raw = self._act(step, index, "read", lambda: self.surface.read(handle))
            self.reads[step.output_key] = (raw or "", step.parse)
        else:
            raise TypeError(f"unknown step: {step!r}")

    def _act(self, step: Step, index: int, verb: str, action) -> Any:
        try:
            return action()
        except Exception as exc:
            timed_out = "timeout" in type(exc).__name__.casefold()
            raise _Abort(
                FailureClass.TIMEOUT if timed_out else FailureClass.SURFACE_ERROR,
                f"{verb} on step {step.id}",
                _short(exc),
                step.id,
                index,
            ) from exc

    # -- checkpoints -------------------------------------------------------

    def _poll_checkpoint(self, step: Step, index: int) -> tuple[bool, Observation]:
        """Poll rather than sleep-then-look: these apps repaint a frame at a
        time and the fast path should stay fast."""
        deadline = time.monotonic() + step.timeout_ms / 1000
        while True:
            obs = self._observe(step, index)
            if evaluate(step.checkpoint, obs, self.surface):
                return True, obs
            if time.monotonic() >= deadline:
                return False, obs
            time.sleep(CHECKPOINT_POLL_S)

    # -- escalation --------------------------------------------------------

    def _escalate(
        self,
        kind: str,
        step: Step | None,
        index: int | None,
        reason: str,
        expected: str,
        observed: str,
    ) -> bool:
        if self.escalator is None:
            return False
        request = EscalationRequest(
            kind=kind,
            run_id=self.run_id,
            capability=self.cap.ref,
            reason=reason,
            step_id=step.id if step else None,
            step_index=index,
            expected=expected,
            observed=self.policy.redact(observed),
        )
        try:
            return bool(self.escalator.escalate(request))
        except Exception as exc:
            raise _Abort(
                FailureClass.ESCALATION_UNRESOLVED,
                f"escalation for {kind}",
                _short(exc),
                step.id if step else None,
                index,
            ) from exc

    # -- outputs -----------------------------------------------------------

    def _collect_outputs(self) -> dict[str, Any]:
        """Only declared outputs leave the executor. A redacted output stays a
        string - a masked balance is not a number and shouldn't pretend to be."""
        outputs: dict[str, Any] = {}
        for key, spec in self.cap.outputs.items():
            if key not in self.reads:
                continue
            raw, parse = self.reads[key]
            outputs[key] = self.policy.redact(raw) if spec.redact else _parse(raw, parse)
        return outputs

    # -- logging and evidence ----------------------------------------------

    def _log(self, step: Step, state: _StepState) -> None:
        self.step_log.append(
            StepLog(
                step_id=step.id,
                intent=step.intent,
                resolved_strategy=state.strategy,
                resolved_tier=state.tier,
                duration_ms=int((time.monotonic() - state.started) * 1000),
                recoveries=state.recoveries,
                checkpoint_ok=state.checkpoint_ok,
                note=state.note or step.note,
            )
        )

    def _failure(self, abort: _Abort) -> Failure:
        if abort.capture:
            self._capture_failure_evidence()
        return Failure(
            step_id=abort.step_id,
            step_index=abort.step_index,
            failure_class=abort.failure_class,
            expected=abort.expected,
            observed=self.policy.redact(abort.observed),
            evidence_ref=str(self.evidence_dir),
            step_log=self.step_log,
        )

    def _capture_failure_evidence(self) -> None:
        """Best effort by design: a failure report is worth more than an
        exception raised while trying to photograph one."""
        try:
            obs = self.surface.observe()
        except Exception:
            return
        try:
            self._screenshot(self.policy.sensitive_handles(obs))
        except Exception:
            pass
        try:
            self._dump_ax(obs)
        except Exception:
            pass

    def _screenshot(self, mask_handles: Iterable[int]) -> None:
        # The protocol takes mask_handles only; the Playwright surface also
        # files the shot under a run id when it's given one.
        kwargs = {}
        try:
            if "run_id" in inspect.signature(self.surface.screenshot).parameters:
                kwargs["run_id"] = self.run_id
        except (TypeError, ValueError):
            pass
        self.surface.screenshot(list(mask_handles), **kwargs)

    def _dump_ax(self, obs: Observation) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "run_id": self.run_id,
            "capability": self.cap.ref,
            "url": obs.url,
            "title": obs.title,
            "nodes": [
                {
                    "handle": n.handle,
                    "role": n.role,
                    "name": self.policy.redact(n.name),
                    "value": self.policy.redact(n.value) if n.value else n.value,
                    "enabled": n.enabled,
                    "frame_path": n.frame_path,
                    "box": None if n.box is None else [n.box.x, n.box.y, n.box.w, n.box.h],
                    "table_id": n.table_id,
                    "row": n.row,
                    "col": n.col,
                }
                for n in obs.nodes
            ],
        }
        path = self.evidence_dir / f"ax_{len(self.step_log):02d}.json"
        path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    def _write_evidence(self, result: ReplayResult) -> ReplayResult:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        lines = result.to_jsonl_lines()
        (self.evidence_dir / "replay.jsonl").write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8"
        )
        return result

    def _slice(self, obs: Observation, predicate) -> str:
        frame = getattr(predicate, "frame_path", "main")
        text = re.sub(r"\s+", " ", obs.text(frame)).strip()
        return self.policy.redact(text[:OBSERVED_CHARS])


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _bind_text(template: str, params: dict[str, str]) -> str:
    out = template
    for key, value in params.items():
        out = out.replace("{{" + key + "}}", value).replace("{{ " + key + " }}", value)
    return out


def _coordinate_candidate(target: Target) -> CoordinateCandidate | None:
    return next((c for c in target.candidates if isinstance(c, CoordinateCandidate)), None)


def _coordinate_tier(target: Target) -> int | None:
    return next(
        (i for i, c in enumerate(target.candidates) if isinstance(c, CoordinateCandidate)),
        None,
    )


def _parse(raw: str, parse: ParseAs) -> Any:
    text = (raw or "").strip()
    if parse is ParseAs.CURRENCY:
        return _to_float(text.replace("$", "").replace(",", ""))
    if parse is ParseAs.NUMBER:
        return _to_float(text)
    return text  # text and date pass through; dates stay as the app rendered them


def _to_float(text: str) -> Any:
    try:
        return float(text)
    except ValueError:
        return text  # unparseable is worth surfacing verbatim, not swallowing


def _short(exc: Exception) -> str:
    return re.sub(r"\s+", " ", f"{type(exc).__name__}: {exc}").strip()[:OBSERVED_CHARS]
