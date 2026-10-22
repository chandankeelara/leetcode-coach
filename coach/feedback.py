"""Feedback to the dev team: what the coach (the model) missed, and every tool that failed it.

* `request_feature(...)`: the coach files a feature / friction / bug. Deduped by key =
  normalised title (+ tool): a repeat bumps `count` and `last_at` instead of a new row.
* `record_friction(user_id, tool, error)`: called by the tool registry (coach/tools.py)
  whenever a coach tool returns {error} or raises. kind = friction, key = tool + the first
  60 normalised characters of the error.
* No student names or code are stored: `example` is cut to 400 characters and refused when it
  looks like code; error texts are cut to 300 characters.
* `/feedback` (admins only) lists them by severity, then count.
"""
import re
from datetime import datetime

from sqlalchemy import select

from .db import session_scope
from .models import CoachRequest, Handout, User

KINDS = ("feature", "friction", "bug")
SEVERITIES = ("blocked", "workaround", "nice")
STATUSES = ("open", "planned", "done")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
EXAMPLE_MAX, TITLE_MAX, WHY_MAX, ERROR_MAX = 400, 200, 1000, 300
_CODE = [r"```", r"^\s*(def|class)\s+\w+.*:\s*$", r"^\s*(for|while|if|elif|return|import|from)\b.*",
         r"\bself\.\w+", r"^\s{4,}\S"]


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def looks_like_code(text):
    """True for a fenced block, or 2+ lines that read like Python statements."""
    t = text or ""
    if "```" in t:
        return True
    lines = [ln for ln in t.splitlines() if ln.strip()]
    hits = sum(1 for ln in lines if any(re.search(p, ln) for p in _CODE[1:]))
    return hits >= 2 or (len(lines) == 1 and bool(re.search(_CODE[1], lines[0])))


def _context(s, user_id):
    from . import state
    h = s.scalars(select(Handout).where(Handout.user_id == user_id, Handout.status == "open")
                  .order_by(Handout.id.desc())).first()
    sess = None
    try:
        sess = state.current_session(s, user_id, datetime.now().date(), create=False)
    except Exception:
        sess = None
    return (sess.id if sess else (h.session_id if h else None)), (h.id if h else None)


def _upsert(s, user_id, kind, key, title, why, example, severity, tool):
    now = datetime.now()
    row = s.scalars(select(CoachRequest).where(CoachRequest.key == key,
                                               CoachRequest.kind == kind)).first()
    if row is not None:
        row.count += 1
        row.last_at = now
        if why and not row.why:
            row.why = why
        if example and not row.example:
            row.example = example
        if SEV_RANK.get(severity, 9) < SEV_RANK.get(row.severity, 9):
            row.severity = severity
        if row.status == "done":
            row.status = "open"          # it came back
        return row, False
    sess_id, hid = _context(s, user_id) if user_id else (None, None)
    row = CoachRequest(user_id=user_id, session_id=sess_id, handout_id=hid, kind=kind, key=key,
                       title=title, why=why, example=example, severity=severity, tool=tool,
                       count=1, first_at=now, last_at=now, status="open")
    s.add(row)
    s.flush()
    return row, True


def request_feature(user_id, title, why="", example="", severity="nice", tool=None,
                    kind="feature"):
    """File one request. ValueError on a bad field or a code-like example. Returns {id, count,
    new}."""
    title = re.sub(r"\s+", " ", (title or "")).strip()[:TITLE_MAX]
    if not title:
        raise ValueError("title is required")
    if kind not in KINDS:
        raise ValueError(f"kind: one of {', '.join(KINDS)}")
    if severity not in SEVERITIES:
        raise ValueError(f"severity: one of {', '.join(SEVERITIES)}")
    example = (example or "").strip()
    if looks_like_code(example):
        raise ValueError("example: describe it in words, no code (his code is never stored)")
    example = example[:EXAMPLE_MAX]
    why = (why or "").strip()[:WHY_MAX]
    tool = (tool or "").strip()[:64] or None
    key = (_norm(title) + ("|" + _norm(tool) if tool else ""))[:300]
    with session_scope() as s:
        row, new = _upsert(s, user_id, kind, key, title, why, example, severity, tool)
        return {"id": row.id, "count": row.count, "new": new}


def record_friction(user_id, tool, error):
    """A coach tool failed: one friction row per (tool, error prefix). Never raises."""
    try:
        err = re.sub(r"\s+", " ", str(error or "")).strip()[:ERROR_MAX]
        if not err:
            return None
        key = f"{_norm(tool)}|{_norm(err)[:60]}"
        title = f"{tool}: {err[:120]}"
        with session_scope() as s:
            row, _ = _upsert(s, user_id, "friction", key, title, err, "", "workaround", tool)
            return row.id
    except Exception:
        return None


def _row(r):
    return {"id": r.id, "kind": r.kind, "title": r.title, "why": r.why, "example": r.example,
            "severity": r.severity, "tool": r.tool, "count": r.count,
            "first_at": r.first_at.isoformat() if r.first_at else None,
            "last_at": r.last_at.isoformat() if r.last_at else None, "status": r.status,
            "session_id": r.session_id, "handout_id": r.handout_id}


def list_requests(kind=None, tool=None, since=None, status=None):
    """Rows ranked by severity (blocked first), then count, then most recent."""
    with session_scope() as s:
        q = select(CoachRequest)
        if kind:
            q = q.where(CoachRequest.kind == kind)
        if tool:
            q = q.where(CoachRequest.tool == tool)
        if status:
            q = q.where(CoachRequest.status == status)
        if since:
            q = q.where(CoachRequest.last_at >= since)
        rows = [_row(r) for r in s.scalars(q)]
        tools = sorted({t for t in s.scalars(select(CoachRequest.tool)) if t})
    rows.sort(key=lambda r: r["last_at"] or "", reverse=True)
    rows.sort(key=lambda r: (SEV_RANK.get(r["severity"], 9), -r["count"]))
    return {"rows": rows, "tools": tools}


def set_status(request_id, status):
    if status not in STATUSES:
        raise ValueError(f"status: one of {', '.join(STATUSES)}")
    with session_scope() as s:
        r = s.get(CoachRequest, request_id)
        if r is None:
            raise ValueError(f"no request {request_id}")
        r.status = status
        return _row(r)


def is_admin(user_id):
    with session_scope() as s:
        u = s.get(User, user_id)
        return bool(u is not None and u.is_admin)
