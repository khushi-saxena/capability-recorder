"""
Capability artifact schema.

Three layers, deliberately separated:

  1. Contract  - name/version/inputs/outputs/risk. What a calling agent needs.
  2. Flow      - ordered, surface-agnostic intents. What a human operator did.
  3. Bindings  - how a target is found on one concrete surface. Ranked candidates.

The split is what makes cross-tenant reuse possible: a variant overlay may
override bindings, never intent. If two tenants need different *steps*, they
are different capabilities and should be recorded separately.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"

TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def referenced_params(text: str) -> set[str]:
    return set(TEMPLATE_RE.findall(text or ""))


def is_pure_template(text: str) -> bool:
    return bool(re.fullmatch(r"\{\{\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\}\}", text or ""))


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------
# Layer 3: bindings
# --------------------------------------------------------------------------


class MatchMode(str, Enum):
    EXACT = "exact"
    CONTAINS = "contains"
    REGEX = "regex"


class AccessibleCandidate(Base):
    """Primary strategy. Role + accessible name is what a screen reader sees,
    which is also what a desktop AX API gives us. Survives markup churn."""

    strategy: Literal["accessible"] = "accessible"
    role: str
    name: str
    name_match: MatchMode = MatchMode.EXACT
    frame_path: str = "main"


class LabelCandidate(Base):
    """For legacy forms where the input has no accessible name but sits next to
    visible label text."""

    strategy: Literal["label"] = "label"
    label_text: str
    control_role: str
    label_match: MatchMode = MatchMode.CONTAINS
    frame_path: str = "main"


class AnchorCandidate(Base):
    """Geometric fallback: nearest control of `role` in `direction` from some
    stable visible text. Table-layout apps need this more often than you'd like."""

    strategy: Literal["anchor"] = "anchor"
    anchor_text: str
    direction: Literal["right", "below", "left", "above"]
    role: str
    frame_path: str = "main"


class TableCellCandidate(Base):
    """Row identified by a cell value, column by its header. Works on the
    nested-table result grids these apps are built out of."""

    strategy: Literal["table_cell"] = "table_cell"
    row_contains: str
    column_header: str
    frame_path: str = "main"


class CoordinateCandidate(Base):
    """Last resort, screenshot-driven. Ratios not pixels so window size varies.
    Resolving on this tier is recorded as a drift signal."""

    strategy: Literal["coordinate"] = "coordinate"
    x_ratio: float = Field(ge=0.0, le=1.0)
    y_ratio: float = Field(ge=0.0, le=1.0)
    frame_path: str = "main"


Candidate = Annotated[
    Union[
        AccessibleCandidate,
        LabelCandidate,
        AnchorCandidate,
        TableCellCandidate,
        CoordinateCandidate,
    ],
    Field(discriminator="strategy"),
]


class Target(Base):
    """A control, described several ways in confidence order. Replay walks the
    list and reports which tier won."""

    describes: str  # human-readable, for review
    candidates: list[Candidate] = Field(min_length=1)


# --------------------------------------------------------------------------
# Predicates - used for checkpoints, outcome detection, recovery triggers
# --------------------------------------------------------------------------


class AXPresent(Base):
    kind: Literal["ax_present"] = "ax_present"
    role: str
    name: str
    name_match: MatchMode = MatchMode.CONTAINS
    frame_path: str = "main"
    negate: bool = False


class TextPresent(Base):
    kind: Literal["text_present"] = "text_present"
    text: str
    frame_path: str = "main"
    negate: bool = False


class UrlMatches(Base):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str
    negate: bool = False


class ValueEquals(Base):
    kind: Literal["value_equals"] = "value_equals"
    target: Target
    expected: str
    negate: bool = False


class AllOf(Base):
    kind: Literal["all_of"] = "all_of"
    of: list["Predicate"] = Field(min_length=1)
    negate: bool = False


Predicate = Annotated[
    Union[AXPresent, TextPresent, UrlMatches, ValueEquals, AllOf],
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------
# Layer 2: flow
# --------------------------------------------------------------------------


class RiskClass(str, Enum):
    SAFE = "safe"  # read-only or reversible
    MUTATING = "mutating"  # creates/edits a record
    IRREVERSIBLE = "irreversible"  # money movement, deletion, external notice


class ParseAs(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    CURRENCY = "currency"
    DATE = "date"


class StepBase(Base):
    id: str
    note: str | None = None
    timeout_ms: int = 10_000
    risk: RiskClass = RiskClass.SAFE
    checkpoint: Predicate | None = None


class NavigateStep(StepBase):
    intent: Literal["navigate"] = "navigate"
    url_template: str


class FillStep(StepBase):
    intent: Literal["fill"] = "fill"
    target: Target
    value_template: str
    sensitive: bool = False


class SelectStep(StepBase):
    intent: Literal["select"] = "select"
    target: Target
    option_template: str


class ActivateStep(StepBase):
    """Click, press, submit. The surface decides how; the flow only says which
    control the operator activated."""

    intent: Literal["activate"] = "activate"
    target: Target


class ReadStep(StepBase):
    intent: Literal["read"] = "read"
    target: Target
    output_key: str
    parse: ParseAs = ParseAs.TEXT


Step = Annotated[
    Union[NavigateStep, FillStep, SelectStep, ActivateStep, ReadStep],
    Field(discriminator="intent"),
]


# --------------------------------------------------------------------------
# Outcomes and recovery
# --------------------------------------------------------------------------


class ExpectedOutcome(Base):
    """A legitimate business answer, declared up front so the caller knows the
    full set of things it can get back. 'No such member' lives here, not in the
    failure path."""

    code: str
    describes: str
    detect: Predicate
    after_step: str | None = None  # only checked from this step onward


class RecoveryAction(str, Enum):
    DISMISS = "dismiss"  # activate a known control and continue
    RETRY_STEP = "retry_step"
    RELOAD = "reload"


class RecoverableCondition(Base):
    """Handled inside the executor. Never surfaces to the caller, always lands
    in the run log."""

    name: str
    detect: Predicate
    action: RecoveryAction
    dismiss_target: Target | None = None
    max_attempts: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def _dismiss_needs_target(self):
        if self.action is RecoveryAction.DISMISS and self.dismiss_target is None:
            raise ValueError("dismiss recovery needs a dismiss_target")
        return self


# --------------------------------------------------------------------------
# Layer 1: contract
# --------------------------------------------------------------------------


class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"


class ParamSpec(Base):
    type: ParamType
    describes: str
    required: bool = True
    pattern: str | None = None
    sensitive: bool = False
    example: str | None = None  # synthetic only, never captured from a real run

    @model_validator(mode="after")
    def _no_sensitive_examples(self):
        if self.sensitive and self.example is not None:
            raise ValueError("sensitive params must not carry an example value")
        return self


class OutputSpec(Base):
    type: ParamType
    describes: str
    redact: bool = False


class AppFingerprint(Base):
    """Identifies the surface an artifact was recorded against. Used to decide
    whether a variant overlay applies."""

    vendor: str
    product: str
    origin: str
    version_hint: str | None = None
    title_pattern: str | None = None
    ax_shape_hash: str | None = None


class ApprovalState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    RETIRED = "retired"


class Provenance(Base):
    """Where the artifact came from. Deliberately does not embed the model
    transcript - that stays in /evidence/ keyed by discovery_run_id."""

    discovery_run_id: str
    model: str
    recorded_at: str
    recorded_by: str
    redaction_applied: bool = True


class Capability(Base):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    name: str
    version: int = Field(ge=1)
    describes: str
    app: AppFingerprint

    inputs: dict[str, ParamSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)

    risk_class: RiskClass = RiskClass.SAFE
    approval_state: ApprovalState = ApprovalState.DRAFT

    flow: list[Step] = Field(min_length=1)
    success: Predicate
    expected_outcomes: list[ExpectedOutcome] = Field(default_factory=list)
    recoverable: list[RecoverableCondition] = Field(default_factory=list)

    provenance: Provenance

    @model_validator(mode="after")
    def _check_coherence(self):
        ids = [s.id for s in self.flow]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")

        used: set[str] = set()
        produced: set[str] = set()
        for step in self.flow:
            if isinstance(step, NavigateStep):
                used |= referenced_params(step.url_template)
            elif isinstance(step, FillStep):
                used |= referenced_params(step.value_template)
                if step.sensitive and not is_pure_template(step.value_template):
                    raise ValueError(
                        f"step {step.id}: sensitive fill must be a bare "
                        f"{{{{param}}}}, not a captured literal"
                    )
            elif isinstance(step, SelectStep):
                used |= referenced_params(step.option_template)
            elif isinstance(step, ReadStep):
                produced.add(step.output_key)

        unknown = used - set(self.inputs)
        if unknown:
            raise ValueError(f"flow references undeclared inputs: {sorted(unknown)}")

        unproduced = set(self.outputs) - produced
        if unproduced:
            raise ValueError(f"declared outputs never read: {sorted(unproduced)}")

        # Risk on the contract must cover the riskiest thing the flow does.
        order = {RiskClass.SAFE: 0, RiskClass.MUTATING: 1, RiskClass.IRREVERSIBLE: 2}
        worst = max((order[s.risk] for s in self.flow), default=0)
        if order[self.risk_class] < worst:
            raise ValueError("risk_class is lower than the riskiest step in the flow")

        outcome_steps = {o.after_step for o in self.expected_outcomes if o.after_step}
        missing = outcome_steps - set(ids)
        if missing:
            raise ValueError(f"expected_outcomes point at unknown steps: {sorted(missing)}")

        return self

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


# --------------------------------------------------------------------------
# Cross-tenant variants
# --------------------------------------------------------------------------


class VariantOverlay(Base):
    """One tenant's deviation from a base capability. Bindings only - if the
    steps differ, that's a different capability."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    tenant_id: str
    base: str  # "member_savings_lookup@1"
    matches: AppFingerprint
    notes: str | None = None
    target_overrides: dict[str, Target] = Field(default_factory=dict)
    url_overrides: dict[str, str] = Field(default_factory=dict)
    extra_recoverable: list[RecoverableCondition] = Field(default_factory=list)


def apply_overlay(cap: Capability, overlay: VariantOverlay) -> Capability:
    """Resolve a base capability for one tenant. Re-validates, so a bad overlay
    fails here rather than mid-replay."""
    if overlay.base != cap.ref:
        raise ValueError(f"overlay targets {overlay.base}, given {cap.ref}")

    known = {s.id for s in cap.flow}
    stray = (set(overlay.target_overrides) | set(overlay.url_overrides)) - known
    if stray:
        raise ValueError(f"overlay references unknown steps: {sorted(stray)}")

    flow: list[Step] = []
    for step in cap.flow:
        patch: dict = {}
        if step.id in overlay.target_overrides and hasattr(step, "target"):
            patch["target"] = overlay.target_overrides[step.id]
        if step.id in overlay.url_overrides and isinstance(step, NavigateStep):
            patch["url_template"] = overlay.url_overrides[step.id]
        flow.append(step.model_copy(update=patch) if patch else step)

    return cap.model_copy(
        update={
            "flow": flow,
            "recoverable": [*cap.recoverable, *overlay.extra_recoverable],
        }
    )
