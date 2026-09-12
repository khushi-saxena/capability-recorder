"""
Operator console.

Deliberately minimal: the handoff mechanism is the real work, this is the
window onto it. Plain HTML, no framework, no build step. It runs in a thread
beside the executor and shares one SessionController, which is the only thing
either side is allowed to touch.

The console never calls the surface. Playwright belongs to the thread that
created it, so live frames are requested through the controller and taken by
the executor thread while it waits.
"""

from __future__ import annotations

import html
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, cli as flask_cli, redirect, request, send_file, url_for

from src.session.controller import OPERATOR, LeaseViolation, SessionController

PORT = 8900
POLL_MS = 2000

_STYLE = """
body { font: 14px/1.5 -apple-system, Segoe UI, Helvetica, Arial, sans-serif;
       margin: 0; padding: 24px; color: #111; background: #f6f6f4; }
h1 { font-size: 18px; margin: 0 0 16px; }
h2 { font-size: 15px; margin: 24px 0 8px; }
table { border-collapse: collapse; width: 100%; background: #fff; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #e2e2de; vertical-align: top; }
th { background: #eceae4; font-weight: 600; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
pre { background: #fff; border: 1px solid #e2e2de; padding: 10px; overflow-x: auto; white-space: pre-wrap; }
a { color: #14506e; }
.lease { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 12px; }
.lease.operator { background: #fde68a; }
.lease.automation { background: #d1fae5; }
form { display: inline-block; margin: 12px 16px 0 0; vertical-align: top; }
button { font: inherit; padding: 6px 14px; }
img.frame { max-width: 100%; border: 1px solid #999; background: #fff; }
.empty { color: #666; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body>{body}</body></html>"
    )


def _age(created_at: str) -> str:
    try:
        started = datetime.fromisoformat(created_at)
    except ValueError:
        return "?"
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _lease_badge(controller: SessionController) -> str:
    return (
        f"<span class='lease {html.escape(controller.lease)}'>lease: "
        f"{html.escape(controller.lease)}</span> since "
        f"<code>{html.escape(controller.holder_since)}</code>"
    )


def create_console(controller: SessionController) -> Flask:
    app = Flask(__name__)

    def _safe_file(path: str | None):
        """Only ever serve what we wrote into the evidence root."""
        if not path:
            abort(404)
        resolved = Path(path).resolve()
        root = controller.evidence_root.resolve()
        if not resolved.is_file() or root not in resolved.parents:
            abort(404)
        return send_file(resolved)

    @app.get("/")
    def index():
        rows = []
        for req in controller.open_requests():
            rows.append(
                "<tr>"
                f"<td><a href='{url_for('detail', request_id=req.id)}'>{html.escape(req.id)}</a></td>"
                f"<td>{html.escape(req.reason.value)}</td>"
                f"<td>{html.escape(req.capability_ref)}</td>"
                f"<td>{html.escape(req.step_id or '-')}"
                f"{'' if req.step_index is None else f' (#{req.step_index})'}</td>"
                f"<td>{html.escape(_age(req.created_at))}</td>"
                "</tr>"
            )
        table = (
            "<table><tr><th>Request</th><th>Reason</th><th>Capability</th>"
            "<th>Step</th><th>Age</th></tr>" + "".join(rows) + "</table>"
            if rows
            else "<p class='empty'>No open requests. Automation is driving.</p>"
        )
        history = "".join(
            f"<tr><td>{html.escape(req.id)}</td><td>{html.escape(req.reason.value)}</td>"
            f"<td>{'resolved' if outcome.resolved else 'unresolved'}</td>"
            f"<td>{html.escape(outcome.operator_notes or '-')}</td></tr>"
            for req, outcome in controller.all_requests()
            if outcome is not None
        )
        if history:
            history = (
                "<h2>Closed</h2><table><tr><th>Request</th><th>Reason</th>"
                "<th>Outcome</th><th>Notes</th></tr>" + history + "</table>"
            )
        return _page(
            "Operator console",
            f"<h1>Operator console</h1><p>{_lease_badge(controller)}</p>{table}{history}",
        )

    @app.get("/request/<request_id>")
    def detail(request_id: str):
        req, outcome = controller.get(request_id)
        if req is None:
            abort(404)

        held_by_operator = controller.lease == OPERATOR and (
            controller.current_request is not None
            and controller.current_request.id == request_id
        )

        live = ""
        if held_by_operator:
            frame_url = url_for("frame", request_id=request_id)
            live = (
                "<h2>Live session</h2>"
                f"<img class='frame' id='frame' src='{frame_url}' alt='live session'>"
                "<script>setInterval(function(){"
                f"document.getElementById('frame').src='{frame_url}?t='+Date.now();"
                f"}}, {POLL_MS});</script>"
            )
        elif req.screenshot_path:
            live = (
                "<h2>Screen when the request opened</h2>"
                f"<img class='frame' src='{url_for('shot', request_id=request_id)}' alt='screen'>"
            )

        actions = ""
        if outcome is None:
            actions = (
                f"<form method='post' action='{url_for('take', request_id=request_id)}'>"
                "<button type='submit'>Take control</button>"
                "<div class='empty'>The browser is headed - drive the real Chromium window.</div>"
                "</form>"
                f"<form method='post' action='{url_for('hand_back', request_id=request_id)}'>"
                "<div><label><input type='radio' name='resolved' value='yes' checked> resolved</label> "
                "<label><input type='radio' name='resolved' value='no'> unresolved</label></div>"
                "<div><textarea name='notes' rows='3' cols='44' "
                "placeholder='What did you do?'></textarea></div>"
                "<button type='submit'>Return control</button>"
                "</form>"
            )
        else:
            actions = (
                f"<p><b>Closed:</b> {'resolved' if outcome.resolved else 'unresolved'} at "
                f"<code>{html.escape(outcome.ended_at)}</code><br>"
                f"{html.escape(outcome.operator_notes or '')}</p>"
            )

        return _page(
            f"Intervention {req.id}",
            f"<h1>{html.escape(req.title)}</h1>"
            f"<p>{_lease_badge(controller)}</p>"
            "<table>"
            f"<tr><th>Request</th><td><code>{html.escape(req.id)}</code></td></tr>"
            f"<tr><th>Run</th><td><code>{html.escape(req.run_id)}</code></td></tr>"
            f"<tr><th>Capability</th><td>{html.escape(req.capability_ref)}</td></tr>"
            f"<tr><th>Step</th><td>{html.escape(req.step_id or '-')}"
            f"{'' if req.step_index is None else f' (#{req.step_index})'}</td></tr>"
            f"<tr><th>Opened</th><td>{html.escape(req.created_at)} ({_age(req.created_at)} ago)</td></tr>"
            f"<tr><th>Expected</th><td><pre>{html.escape(req.expected)}</pre></td></tr>"
            f"<tr><th>Observed</th><td><pre>{html.escape(req.observed)}</pre></td></tr>"
            f"<tr><th>AX snapshot</th><td><code>{html.escape(req.ax_snapshot_path or '-')}</code></td></tr>"
            "</table>"
            f"{actions}{live}"
            f"<p><a href='{url_for('index')}'>&larr; all requests</a></p>",
        )

    @app.post("/request/<request_id>/take")
    def take(request_id: str):
        try:
            controller.take_control(request_id)
        except LeaseViolation as exc:
            return _page("Lease", f"<h1>Cannot take control</h1><pre>{html.escape(str(exc))}</pre>"), 409
        return redirect(url_for("detail", request_id=request_id))

    @app.post("/request/<request_id>/return")
    def hand_back(request_id: str):
        resolved = request.form.get("resolved") == "yes"
        notes = (request.form.get("notes") or "").strip()
        try:
            controller.return_to_automation(resolved, notes)
        except LeaseViolation as exc:
            return _page("Lease", f"<h1>Cannot return control</h1><pre>{html.escape(str(exc))}</pre>"), 409
        return redirect(url_for("detail", request_id=request_id))

    @app.get("/request/<request_id>/frame")
    def frame(request_id: str):
        path = controller.request_frame()
        if path is None:
            abort(503)  # nobody is waiting to take the picture
        return _safe_file(path)

    @app.get("/request/<request_id>/shot")
    def shot(request_id: str):
        req, _ = controller.get(request_id)
        return _safe_file(req.screenshot_path if req else None)

    @app.get("/status")
    def status():
        return controller.status()

    return app


def serve(controller: SessionController, port: int = PORT) -> threading.Thread:
    """Runs the console in a daemon thread beside the executor."""
    # The development server greets stdout, which is where the CLI writes the
    # run result. Anything a caller pipes into a JSON parser has to stay clean.
    flask_cli.show_server_banner = lambda *args, **kwargs: None
    app = create_console(controller)
    thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False),
        name=f"operator-console:{port}",
        daemon=True,
    )
    thread.start()
    return thread
