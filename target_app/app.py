"""Pioneer Heritage Credit Union — Member Servicing Console (circa 2006).

A deliberately hostile automation target: frames, nested tables, opaque
field names, and deterministic runtime conditions that can be injected
for a demo run.
"""

from __future__ import annotations

import os
import time
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import (
    Flask,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "phcu-servicing-console-2006")

SESSION_COOKIE = "PHCUSESS"
SESSION_TTL = int(os.environ.get("SESSION_TTL", "3600"))

_SEED_MEMBERS = {
    "12345": {
        "name": "Dana Whitfield",
        "restricted": False,
        "accounts": [
            {"product": "SAVINGS", "balance": Decimal("4210.55")},
            {"product": "CHECKING", "balance": Decimal("812.03")},
        ],
    },
    "23456": {
        "name": "Marcus Oyelaran",
        "restricted": False,
        "accounts": [
            {"product": "CHECKING", "balance": Decimal("1904.17")},
        ],
    },
    "34567": {
        "name": "Avery Tolliver",
        "restricted": True,
        "accounts": [
            {"product": "SAVINGS", "balance": Decimal("100.00")},
            {"product": "CHECKING", "balance": Decimal("50.00")},
        ],
    },
}

MEMBERS: dict = {}


def reset_members() -> None:
    MEMBERS.clear()
    MEMBERS.update(deepcopy(_SEED_MEMBERS))


reset_members()


def money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _with_query(url: str, **updates) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, val in updates.items():
        if val is None:
            query.pop(key, None)
        else:
            query[key] = val
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _content_url() -> str:
    full = request.full_path
    if full.endswith("?"):
        full = full[:-1]
    return full


def _inject_state() -> dict:
    return session.setdefault(
        "inject",
        {"slow": False, "interstitial": None, "expire": False},
    )


def _session_issued_at() -> float | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _session_is_expired() -> bool:
    if not session.get("operator"):
        return False
    inject = session.get("inject") or {}
    if inject.get("expire"):
        return True
    issued = _session_issued_at()
    if issued is None:
        return True
    return (time.time() - issued) > SESSION_TTL


def _login_redirect(expired: bool = False):
    target = url_for("login", expired="1") if expired else url_for("login")
    html = render_template("breakout.html", target=target)
    resp = make_response(html)
    resp.delete_cookie(SESSION_COOKIE)
    session.pop("operator", None)
    return resp


def _stamp_session(response, issued_at: float | None = None):
    ts = issued_at if issued_at is not None else time.time()
    response.set_cookie(
        SESSION_COOKIE,
        str(ts),
        path="/",
        httponly=True,
        samesite="Lax",
    )
    return response


def require_session(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("operator"):
            return _login_redirect(expired=False)
        if _session_is_expired():
            return _login_redirect(expired=True)
        return view(*args, **kwargs)

    return wrapped


def content_conditions(view):
    """Apply slow-load and interstitial gates inside the content frame."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if request.method == "GET":
            inject = _inject_state()
            slow = request.args.get("slow") == "1" or bool(inject.get("slow"))
            loaded = request.args.get("_loaded") == "1"
            if slow and not loaded:
                next_url = _with_query(_content_url(), _loaded="1")
                return render_template("loading.html", next_url=next_url)

            interstitial = request.args.get("interstitial") or inject.get("interstitial")
            if interstitial in ("1", "unknown"):
                acked = session.get("acked_interstitials") or []
                if interstitial not in acked:
                    return render_template(
                        "interstitial.html",
                        variant=interstitial,
                        next_url=_content_url(),
                    )
        return view(*args, **kwargs)

    return wrapped


@app.context_processor
def _template_helpers():
    return {"money": money, "operator": session.get("operator")}


@app.route("/", methods=["GET", "POST"])
def login():
    error = None
    if request.args.get("expired") == "1":
        error = "Your session has expired"
    if request.method == "GET" and request.args.get("expired") != "1":
        session.pop("operator", None)
    if request.method == "POST":
        operator = (request.form.get("fld_001") or "").strip() or "TELLER"
        session.clear()
        session["operator"] = operator
        session["inject"] = {"slow": False, "interstitial": None, "expire": False}
        session["acked_interstitials"] = []
        session["pending_subaccount"] = None
        resp = redirect(url_for("console"))
        return _stamp_session(resp)
    return render_template("login.html", error=error)


@app.route("/console")
@require_session
def console():
    content_src = url_for("member_search")
    return render_template("frameset.html", content_src=content_src)


@app.route("/chrome/banner")
@require_session
def chrome_banner():
    return render_template("banner.html")


@app.route("/chrome/nav")
@require_session
def chrome_nav():
    return render_template("nav.html")


@app.route("/servicing/member/search", methods=["GET", "POST"])
@require_session
@content_conditions
def member_search():
    if request.method == "POST":
        q = (request.form.get("fld_001") or "").strip()
        return redirect(url_for("member_results", q=q))
    return render_template("search.html")


@app.route("/servicing/member/results")
@require_session
@content_conditions
def member_results():
    q = (request.args.get("q") or "").strip()
    error = None
    results = []
    if not q.isdigit():
        error = "Member ID must be numeric"
    elif q == "99999":
        error = "No records match your search"
    else:
        member = MEMBERS.get(q)
        if member is None:
            error = "No records match your search"
        else:
            results = [{"id": q, "name": member["name"]}]
    return render_template("results.html", q=q, error=error, results=results)


@app.route("/servicing/member/<member_id>")
@require_session
@content_conditions
def member_detail(member_id):
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template(
            "results.html",
            q=member_id,
            error="No records match your search",
            results=[],
        )
    if member["restricted"]:
        return render_template("unauthorized.html", member_id=member_id)
    return render_template("member.html", member_id=member_id, member=member)


@app.route("/servicing/member/<member_id>/subaccount/new", methods=["GET", "POST"])
@require_session
@content_conditions
def subaccount_new(member_id):
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template(
            "results.html",
            q=member_id,
            error="No records match your search",
            results=[],
        )
    if member["restricted"]:
        return render_template("unauthorized.html", member_id=member_id)

    error = None
    if request.method == "POST":
        product = (request.form.get("fld_001") or "").strip().upper()
        amount_raw = (request.form.get("fld_002") or "").strip()
        if product not in ("SAVINGS", "CHECKING", "MONEY MARKET"):
            error = "Select a valid product"
        else:
            try:
                amount = Decimal(amount_raw.replace(",", "").replace("$", ""))
                if amount < 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                error = "Opening amount is not valid"
                amount = None
            if error is None:
                session["pending_subaccount"] = {
                    "member_id": member_id,
                    "product": product,
                    "amount": str(amount),
                }
                return redirect(url_for("subaccount_confirm", member_id=member_id))
    return render_template("subaccount_new.html", member_id=member_id, member=member, error=error)


@app.route("/servicing/member/<member_id>/subaccount/confirm", methods=["GET", "POST"])
@require_session
@content_conditions
def subaccount_confirm(member_id):
    member = MEMBERS.get(member_id)
    if member is None:
        return render_template(
            "results.html",
            q=member_id,
            error="No records match your search",
            results=[],
        )
    if member["restricted"]:
        return render_template("unauthorized.html", member_id=member_id)

    pending = session.get("pending_subaccount") or {}
    if pending.get("member_id") != member_id:
        return redirect(url_for("subaccount_new", member_id=member_id))

    amount = Decimal(pending["amount"])
    if request.method == "POST":
        member["accounts"].append(
            {"product": pending["product"], "balance": amount}
        )
        session["pending_subaccount"] = None
        return redirect(url_for("member_detail", member_id=member_id))

    return render_template(
        "subaccount_confirm.html",
        member_id=member_id,
        member=member,
        product=pending["product"],
        amount=amount,
    )


@app.route("/interstitial/ack", methods=["POST"])
@require_session
def interstitial_ack():
    next_url = request.form.get("fld_001") or url_for("member_search")
    variant = request.form.get("fld_002") or "1"
    acked = list(session.get("acked_interstitials") or [])
    if variant not in acked:
        acked.append(variant)
    session["acked_interstitials"] = acked
    if not next_url.startswith("/"):
        next_url = url_for("member_search")
    return redirect(next_url)


@app.route("/admin/inject", methods=["GET", "POST"])
def admin_inject():
    inject = _inject_state()
    if request.method == "POST":
        mode = request.form.get("fld_001") or "off"
        mapping = {
            "off": {"slow": False, "interstitial": None, "expire": False},
            "slow": {"slow": True, "interstitial": None, "expire": False},
            "interstitial": {"slow": False, "interstitial": "1", "expire": False},
            "unknown": {"slow": False, "interstitial": "unknown", "expire": False},
            "expire": {"slow": False, "interstitial": None, "expire": True},
            "clear": {"slow": False, "interstitial": None, "expire": False},
        }
        chosen = mapping.get(mode, mapping["off"])
        session["inject"] = dict(chosen)
        if mode == "clear":
            session["acked_interstitials"] = []
            reset_members()
        if mode == "interstitial" or mode == "unknown":
            session["acked_interstitials"] = []

        issued = time.time()
        if chosen["expire"]:
            issued = time.time() - SESSION_TTL - 5
        resp = make_response(
            render_template(
                "admin_inject.html",
                inject=session["inject"],
                ttl=SESSION_TTL,
                saved=True,
            )
        )
        if session.get("operator"):
            _stamp_session(resp, issued_at=issued)
        return resp

    return render_template(
        "admin_inject.html",
        inject=inject,
        ttl=SESSION_TTL,
        saved=False,
    )


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8800)
