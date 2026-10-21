"""Browser-extension ingest: a submission made on leetcode.com becomes the same `submission`
event the web editor pushes. Auth is a random token kept in the config table. Never receives
statement text: only slug/number, code, language, verdict, runtime text."""
import hmac
import secrets
from datetime import date

from sqlalchemy import select

from . import service
from .db import session_scope
from .models import Config, Handout, Problem

TOKEN_KEY = "ext_token"


def ext_token_get_or_create():
    with session_scope() as s:
        row = s.get(Config, TOKEN_KEY)
        if row is None or not row.value:
            row = Config(key=TOKEN_KEY, value=secrets.token_urlsafe(32))
            s.add(row)
        return row.value


def ext_token_rotate():
    tok = secrets.token_urlsafe(32)
    with session_scope() as s:
        row = s.get(Config, TOKEN_KEY)
        if row is None:
            s.add(Config(key=TOKEN_KEY, value=tok))
        else:
            row.value = tok
    return tok


def token_ok(token):
    if not isinstance(token, str) or not token:
        return False
    with session_scope() as s:
        row = s.get(Config, TOKEN_KEY)
        real = row.value if row is not None else None
    if not real:
        return False
    return hmac.compare_digest(token.encode("utf-8"), str(real).encode("utf-8"))


def open_handout(user_id):
    """(handout_id, problem_number) of today's open handout, or None. Same rule as the web."""
    with session_scope() as s:
        h = s.scalars(select(Handout).where(
            Handout.user_id == user_id, Handout.status == "open",
            Handout.slot != "imported").order_by(Handout.id.desc())).first()
        if h is None or h.handed_at.date() != date.today():
            return None
        return h.id, h.problem_number


def open_problem_number(token):
    """None if no open handout; raises PermissionError on a bad token."""
    if not token_ok(token):
        raise PermissionError("bad token")
    uid = service.default_user_id()
    if uid is None:
        return None
    oh = open_handout(uid)
    return oh[1] if oh else None


def _number_for(slug, number):
    with session_scope() as s:
        if slug:
            n = s.scalars(select(Problem.number).where(Problem.lc_slug == slug)).first()
            if n is not None:
                return n
        if number is not None:
            try:
                n = int(number)
            except (TypeError, ValueError):
                return None
            return s.scalars(select(Problem.number).where(Problem.number == n)).first()
    return None


def ingest_submission(token, slug, code, language, verdict, extra=None):
    """Returns {ok, handout_id|None, lint, ...}. Raises PermissionError on a bad token."""
    if not token_ok(token):
        raise PermissionError("bad token")
    extra = extra or {}
    uid = service.default_user_id()
    if uid is None:
        return {"ok": False, "handout_id": None, "lint": [], "reason": "no user"}
    num = _number_for(slug, extra.get("number"))
    oh = open_handout(uid)
    if num is None or oh is None or oh[1] != num:
        return {"ok": False, "handout_id": None, "lint": [], "reason": "not the open problem"}
    handout_id = oh[0]
    code = code or ""
    language = (language or "").strip()
    lint_out = []
    if language.lower() in ("python", "python3"):
        from .lint import lint
        lint_out = [{"line": ln, "kind": kind, "msg": msg} for ln, kind, msg in lint(code)]
    verdict = (verdict or "").strip()
    payload = {"code": code, "lint": lint_out, "verdict": verdict, "source": "leetcode",
               "language": language}
    if extra.get("runtime"):
        payload["runtime"] = str(extra["runtime"])[:500]
    service.push_event(uid, "submission", payload)
    service.post_message(uid, "user", f"Submitted on LeetCode: {verdict or 'unknown'}",
                         handout_id=handout_id)
    return {"ok": True, "handout_id": handout_id, "lint": lint_out}
