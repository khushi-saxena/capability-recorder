"""
Command line entry point.

    python -m src.cli replay artifacts/member_savings_lookup.v1.json --member_id 12345

Capability inputs are passed as long flags named after the input keys, so the
contract in the artifact is the CLI's own argument list. Anything the artifact
doesn't declare is rejected by the executor's input validation, not here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from src.operator.console import PORT as CONSOLE_PORT
from src.policy.policy import Policy
from src.replay.executor import replay
from src.schema import Capability
from src.session.controller import SessionController


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("replay", help="replay a capability artifact")
    run.add_argument("artifact", type=Path)
    run.add_argument("--policy", type=Path, default=None, help="policy.yaml to enforce")
    run.add_argument("--run-id", default=None, help="evidence folder name")
    run.add_argument("--headless", action="store_true")
    run.add_argument("--operator", default="op1", help="operator id for the app login")
    run.add_argument(
        "--no-login",
        action="store_true",
        help="skip the sign-in the target app needs before the flow can run",
    )
    run.add_argument(
        "--operator-console",
        action="store_true",
        help="hand stuck steps to a human through the console on --console-port",
    )
    run.add_argument("--console-port", type=int, default=CONSOLE_PORT)

    args, extra = parser.parse_known_args(argv)
    if args.command != "replay":
        parser.error(f"unknown command: {args.command}")

    try:
        params = _parse_params(extra)
    except ValueError as exc:
        parser.error(str(exc))

    capability = Capability.model_validate_json(args.artifact.read_text(encoding="utf-8"))
    policy = Policy(args.policy)

    # Imported here so `--help` and artifact errors don't pay for Playwright.
    from src.surface.playwright_surface import PlaywrightSurface

    surface = PlaywrightSurface(headless=args.headless)
    controller = None
    if args.operator_console:
        from src.operator.console import serve

        controller = SessionController(surface, policy=policy)
        serve(controller, port=args.console_port)
        print(
            f"operator console on http://127.0.0.1:{args.console_port}",
            file=sys.stderr,
        )

    try:
        if not args.no_login:
            _sign_in(surface, capability.app.origin, args.operator)
        result = replay(
            capability,
            params,
            surface,
            policy,
            escalator=controller,
            run_id=args.run_id,
        )
    finally:
        surface.close()

    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return 0 if result.kind != "failure" else 1


def _parse_params(extra: list[str]) -> dict[str, Any]:
    """`--member_id 12345` and `--member_id=12345` both work."""
    params: dict[str, Any] = {}
    index = 0
    while index < len(extra):
        token = extra[index]
        if not token.startswith("--"):
            raise ValueError(f"unexpected argument: {token}")
        if "=" in token:
            key, value = token[2:].split("=", 1)
            index += 1
        else:
            key = token[2:]
            if index + 1 >= len(extra) or extra[index + 1].startswith("--"):
                raise ValueError(f"missing value for {token}")
            value = extra[index + 1]
            index += 2
        params[key.replace("-", "_")] = value
    return params


def _sign_in(surface, origin: str, operator: str) -> None:
    """The target app gates every servicing screen behind a session. This is
    session setup, not part of the capability - the artifact starts after it."""
    surface.navigate(origin + "/")
    obs = surface.observe()
    boxes = [n for n in obs.nodes if n.role == "textbox"]
    button = next((n for n in obs.nodes if n.role == "button"), None)
    if not boxes or button is None:
        return  # already signed in, or this surface has no login screen
    surface.fill(boxes[0].handle, operator)
    if len(boxes) > 1:
        surface.fill(boxes[1].handle, "x")
    surface.activate(button.handle)


if __name__ == "__main__":
    sys.exit(main())
