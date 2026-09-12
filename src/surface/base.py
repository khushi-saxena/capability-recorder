"""
Surface layer.

The seam of the whole system: everything above this file speaks in accessibility
terms (role, name, value, geometry). Nothing above it knows what a browser is.

A Surface can observe and act. The resolver turns a schema Target into a
concrete handle on one observation. Because resolution is a pure function over
an AX snapshot, it is testable without a browser - which is most of why the
seam sits here rather than inside the executor.

Adding a desktop surface means implementing Surface over UIAutomation/AX API.
The resolver, executor, and artifacts stay untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Protocol, runtime_checkable

from src.schema import (
    AccessibleCandidate,
    AnchorCandidate,
    Candidate,
    CoordinateCandidate,
    LabelCandidate,
    MatchMode,
    TableCellCandidate,
    Target,
)


@dataclass(frozen=True)
class Box:
    """Ratios of the viewport, not pixels. Window size varies per operator."""

    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass(frozen=True)
class AXNode:
    handle: int  # valid for this observation only
    role: str
    name: str = ""
    value: str | None = None
    enabled: bool = True
    frame_path: str = "main"
    box: Box | None = None
    # populated for grid content; None elsewhere
    table_id: str | None = None
    row: int | None = None
    col: int | None = None


@dataclass(frozen=True)
class Observation:
    url: str
    title: str
    nodes: tuple[AXNode, ...]
    screenshot_ref: str | None = None

    def text(self, frame_path: str = "main") -> str:
        """Flattened visible text for predicate checks."""
        parts = [
            n.name if n.name else (n.value or "")
            for n in self.nodes
            if _in_frame(n.frame_path, frame_path)
        ]
        return "\n".join(p for p in parts if p)


def ax_snapshot(obs: Observation, redact: Callable[[str], str] | None = None) -> dict:
    """Serialisable form of an observation, for evidence. `redact` is applied to
    every name and value, so a snapshot can be written from a screen holding
    member data without copying it into the run folder."""
    clean = redact or (lambda s: s)
    return {
        "url": obs.url,
        "title": obs.title,
        "nodes": [
            {
                "handle": n.handle,
                "role": n.role,
                "name": clean(n.name),
                "value": clean(n.value) if n.value else n.value,
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


@runtime_checkable
class Surface(Protocol):
    """Minimal verb set. Deliberately small - anything a legacy web app and a
    desktop app can both do, and nothing more."""

    def observe(self) -> Observation: ...
    def navigate(self, url: str) -> None: ...
    def activate(self, handle: int) -> None: ...
    def fill(self, handle: int, text: str) -> None: ...
    def select(self, handle: int, option: str) -> None: ...
    def read(self, handle: int) -> str: ...
    def activate_at(self, x_ratio: float, y_ratio: float) -> None: ...
    def screenshot(self, mask_handles: Iterable[int] = ()) -> str: ...


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    handle: int
    tier: int  # index in the candidate list; >0 is a drift signal
    strategy: str
    describes: str


class TargetUnresolved(Exception):
    def __init__(self, target: Target, tried: list[str]):
        self.target = target
        self.tried = tried
        super().__init__(f"could not resolve {target.describes!r}; tried {tried}")


def _in_frame(node_frame: str, wanted: str) -> bool:
    # "main" matches everything under main; exact paths match themselves.
    return node_frame == wanted or node_frame.startswith(wanted + "/")


def _matches(text: str, wanted: str, mode: MatchMode) -> bool:
    text, wanted = (text or "").strip(), (wanted or "").strip()
    if mode is MatchMode.EXACT:
        return text.casefold() == wanted.casefold()
    if mode is MatchMode.CONTAINS:
        return wanted.casefold() in text.casefold()
    return re.search(wanted, text) is not None


def _candidates_in_frame(obs: Observation, frame_path: str) -> list[AXNode]:
    return [n for n in obs.nodes if _in_frame(n.frame_path, frame_path)]


def _by_accessible(c: AccessibleCandidate, obs: Observation) -> int | None:
    hits = [
        n
        for n in _candidates_in_frame(obs, c.frame_path)
        if n.role == c.role and _matches(n.name, c.name, c.name_match)
    ]
    # Ambiguity is a resolution failure, not a coin flip.
    return hits[0].handle if len(hits) == 1 else None


def _by_label(c: LabelCandidate, obs: Observation) -> int | None:
    nodes = _candidates_in_frame(obs, c.frame_path)
    labels = [n for n in nodes if _matches(n.name, c.label_text, c.label_match)]
    controls = [n for n in nodes if n.role == c.control_role and n.box]
    for lab in labels:
        if lab.box is None:
            continue
        # nearest control on the same visual row, to the right
        same_row = [
            n
            for n in controls
            if abs(n.box.cy - lab.box.cy) < max(lab.box.h, 0.02)
            and n.box.x >= lab.box.x
        ]
        if same_row:
            return min(same_row, key=lambda n: n.box.x).handle
    return None


_DIRECTION_KEY = {
    "right": lambda a, n: (n.box.x - a.box.x, abs(n.box.cy - a.box.cy)),
    "left": lambda a, n: (a.box.x - n.box.x, abs(n.box.cy - a.box.cy)),
    "below": lambda a, n: (n.box.y - a.box.y, abs(n.box.cx - a.box.cx)),
    "above": lambda a, n: (a.box.y - n.box.y, abs(n.box.cx - a.box.cx)),
}


def _by_anchor(c: AnchorCandidate, obs: Observation) -> int | None:
    nodes = [n for n in _candidates_in_frame(obs, c.frame_path) if n.box]
    anchors = [n for n in nodes if _matches(n.name, c.anchor_text, MatchMode.CONTAINS)]
    key = _DIRECTION_KEY[c.direction]
    for anchor in anchors:
        forward = [
            n for n in nodes if n.role == c.role and n is not anchor and key(anchor, n)[0] > 0
        ]
        if forward:
            return min(forward, key=lambda n: key(anchor, n)).handle
    return None


def _by_table_cell(c: TableCellCandidate, obs: Observation) -> int | None:
    nodes = _candidates_in_frame(obs, c.frame_path)
    for table_id in {n.table_id for n in nodes if n.table_id}:
        cells = [n for n in nodes if n.table_id == table_id]
        header = next(
            (
                n
                for n in cells
                if n.role == "columnheader"
                and _matches(n.name, c.column_header, MatchMode.CONTAINS)
            ),
            None,
        )
        if header is None or header.col is None:
            continue
        rows = {
            n.row
            for n in cells
            if n.row is not None
            and _matches(n.name or n.value or "", c.row_contains, MatchMode.CONTAINS)
        }
        if len(rows) != 1:
            continue
        row = rows.pop()
        hit = next(
            (n for n in cells if n.row == row and n.col == header.col),
            None,
        )
        if hit:
            return hit.handle
    return None


def resolve(target: Target, obs: Observation) -> Resolution:
    """Walk candidates in order. First unambiguous hit wins; the tier it won on
    is returned so the caller can log drift."""
    tried: list[str] = []
    for tier, cand in enumerate(target.candidates):
        handle = _dispatch(cand, obs)
        tried.append(cand.strategy)
        if handle is not None:
            return Resolution(handle, tier, cand.strategy, target.describes)
    raise TargetUnresolved(target, tried)


def _dispatch(cand: Candidate, obs: Observation) -> int | None:
    if isinstance(cand, AccessibleCandidate):
        return _by_accessible(cand, obs)
    if isinstance(cand, LabelCandidate):
        return _by_label(cand, obs)
    if isinstance(cand, AnchorCandidate):
        return _by_anchor(cand, obs)
    if isinstance(cand, TableCellCandidate):
        return _by_table_cell(cand, obs)
    if isinstance(cand, CoordinateCandidate):
        return None  # handled by the executor via activate_at, no handle exists
    raise TypeError(f"unknown candidate strategy: {cand!r}")


def bind_templates(target: Target, params: dict[str, str]) -> Target:
    """Targets can contain {{param}} (a grid row keyed by member id, say).
    Substituted at replay time, before resolution."""

    def sub(text: str) -> str:
        out = text
        for k, v in params.items():
            out = out.replace("{{" + k + "}}", str(v)).replace("{{ " + k + " }}", str(v))
        return out

    fields = ("name", "label_text", "anchor_text", "row_contains", "column_header")
    new = []
    for c in target.candidates:
        patch = {f: sub(getattr(c, f)) for f in fields if hasattr(c, f)}
        new.append(c.model_copy(update=patch) if patch else c)
    return target.model_copy(update={"candidates": new})
