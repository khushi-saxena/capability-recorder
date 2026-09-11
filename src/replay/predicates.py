"""
Predicate evaluation.

Checkpoints, outcome detection and recovery triggers are all the same small
language, evaluated against one observation. Kept pure over an Observation (the
surface is only touched for value_equals, which has to read a live control) so
the executor's decisions are testable without a browser.
"""

from __future__ import annotations

import re

from src.schema import (
    AllOf,
    AXPresent,
    Predicate,
    TextPresent,
    UrlMatches,
    ValueEquals,
)
from src.surface.base import (
    Observation,
    Surface,
    TargetUnresolved,
    _in_frame,
    _matches,
    resolve,
)


def evaluate(predicate: Predicate, observation: Observation, surface: Surface) -> bool:
    """True if the predicate holds. `negate` is applied here, once, so nested
    predicates each honour their own flag."""
    return _holds(predicate, observation, surface) != predicate.negate


def describe(predicate: Predicate) -> str:
    """Human-readable form, used as the `expected` side of a failure."""
    if isinstance(predicate, AXPresent):
        body = f"{predicate.role} named {predicate.name!r} in {predicate.frame_path}"
    elif isinstance(predicate, TextPresent):
        body = f"text {predicate.text!r} in {predicate.frame_path}"
    elif isinstance(predicate, UrlMatches):
        body = f"url matching {predicate.pattern!r}"
    elif isinstance(predicate, ValueEquals):
        body = f"{predicate.target.describes} equals {predicate.expected!r}"
    elif isinstance(predicate, AllOf):
        body = "all of [" + "; ".join(describe(p) for p in predicate.of) + "]"
    else:
        raise TypeError(f"unknown predicate: {predicate!r}")
    return f"not ({body})" if predicate.negate else body


def _holds(predicate: Predicate, obs: Observation, surface: Surface) -> bool:
    if isinstance(predicate, AXPresent):
        return any(
            node.role == predicate.role
            and _matches(node.name, predicate.name, predicate.name_match)
            for node in obs.nodes
            if _in_frame(node.frame_path, predicate.frame_path)
        )

    if isinstance(predicate, TextPresent):
        return predicate.text.casefold() in obs.text(predicate.frame_path).casefold()

    if isinstance(predicate, UrlMatches):
        return re.search(predicate.pattern, obs.url) is not None

    if isinstance(predicate, ValueEquals):
        try:
            resolution = resolve(predicate.target, obs)
        except TargetUnresolved:
            # Can't read what isn't there; that's a false predicate, not an error.
            return False
        return surface.read(resolution.handle).strip() == predicate.expected.strip()

    if isinstance(predicate, AllOf):
        return all(evaluate(p, obs, surface) for p in predicate.of)

    raise TypeError(f"unknown predicate: {predicate!r}")
