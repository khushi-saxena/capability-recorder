"""Policy checks for capability navigation and surface actions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from src.surface.base import Observation


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class RequireConfirmation:
    reason: str


@dataclass(frozen=True)
class Deny:
    reason: str


Decision = Allow | RequireConfirmation | Deny


class Policy:
    """Load and enforce the workspace policy for a target application."""

    def __init__(self, path: str | Path | None = None):
        policy_path = Path(path) if path is not None else Path(__file__).parents[2] / "policy.yaml"
        with policy_path.open(encoding="utf-8") as policy_file:
            config = yaml.safe_load(policy_file) or {}

        self.allowed_origins = frozenset(config.get("allowed_origins", []))
        self.allowed_paths = tuple(re.compile(pattern) for pattern in config.get("allowed_paths", []))
        self.allowed_intents = frozenset(config.get("allowed_intents", []))
        self.risk_rules = tuple(config.get("risk_rules", []))
        self.sensitive_patterns = tuple(
            re.compile(pattern, re.IGNORECASE)
            for pattern in config.get("sensitive_name_patterns", [])
        )

    def check_navigate(self, url: str) -> Decision:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.allowed_origins:
            return Deny(f"origin is not allowed: {origin}")
        if not any(pattern.search(parts.path) for pattern in self.allowed_paths):
            return Deny(f"path is not allowed: {parts.path}")
        return Allow()

    def check_action(self, intent: str, url: str, risk: str) -> Decision:
        if intent not in self.allowed_intents:
            return Deny(f"intent is not allowed: {intent}")

        effective_risk = risk
        path = urlsplit(url).path
        for rule in self.risk_rules:
            if rule.get("intent") != intent:
                continue
            if re.search(rule["url_pattern"], path):
                effective_risk = self._higher_risk(effective_risk, rule["risk"])

        if effective_risk == "irreversible":
            return RequireConfirmation(f"irreversible action: {intent} {path}")
        return Allow()

    def redact(self, text: str) -> str:
        redacted = text
        for pattern in self.sensitive_patterns:
            redacted = pattern.sub("[REDACTED]", redacted)
        redacted = re.sub(r"\b\d{3}[- ]\d{2}[- ]\d{4}\b", "[REDACTED]", redacted)
        return re.sub(r"\d{9,}", "[REDACTED]", redacted)

    def sensitive_handles(self, observation: Observation) -> list[int]:
        return [
            node.handle
            for node in observation.nodes
            if any(pattern.search(node.name) for pattern in self.sensitive_patterns)
        ]

    @staticmethod
    def _higher_risk(left: str, right: str) -> str:
        levels = {"safe": 0, "mutating": 1, "irreversible": 2}
        return right if levels[right] > levels[left] else left
