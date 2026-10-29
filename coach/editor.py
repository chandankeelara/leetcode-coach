"""Coach tools on his code editor: draft sync, stub, annotations, coach comments, scratch tabs.

Re-exported by coach/service.py (see the "editor tools" block there), so callers use
`service.read_code(...)` etc. Every coach-side write emits a ui_event so the page refreshes:
`editor` (stub), `annotations`, `code_ops`, `scratch`. Draft saves emit nothing (too chatty).

Leak guard: all coach text goes through service.leak_words, with camelCase split so
identifiers count. Words the problem title or HIS OWN current code already contains are
exempt (he named them; RULE ZERO is about what he has not named).
"""
import re
from datetime import datetime

from sqlalchemy import select, update

from .db import session_scope
from .models import Annotation, CodeDraft, CodeOp, Event, Handout, Problem, Scratch

ANNOTATION_KINDS = ("question", "followup", "note", "highlight")
DRAFT_MAX = 200_000          # chars
TEXT_MAX = 400               # annotation / highlight text
COMMENT_MAX = 160            # add_comment: single line
TITLE_MAX = 60               # scratch title
CODE_MAX = 20_000            # stub / scratch code
EDITOR_UI_KINDS = frozenset({"annotations", "code_ops", "scratch"})


def _svc():
    from . import service
    return service


def _iso(dt):
    return dt.isoformat() if dt else None


def _split_camel(text):
    """maxSlidingWindow -> max Sliding Window, so identifiers hit the leak guard."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")


def _handout(s, user_id, handout_id):
    h = s.get(Handout, handout_id) if handout_id is not None else None
    if h is None or h.user_id != user_id:
        raise ValueError(f"no handout {handout_id}")
    return h


def _draft(s, handout_id):
    return s.scalars(select(CodeDraft).where(CodeDraft.handout_id == handout_id)).first()


def _guard(s, h, text, own_code=True):
    """ValueError('leak: ...') when `text` names a technique or a difficulty. Exempt: words in
    the title and (own_code) in his current draft."""
    title = s.get(Problem, h.problem_number).title
    exempt = _split_camel(title)
    if own_code:
        d = _draft(s, h.id)
        if d is not None and d.code:
            exempt += "\n" + _split_camel(d.code)
    bad = _svc().leak_words(_split_camel(text), exempt, s, h if own_code else None)
    if bad:
        raise ValueError("leak: " + ", ".join(bad))
    _svc().rule_zero_check(s, h.user_id, text, h)


def _one_line(text, limit, what):
    t = re.sub(r"\s+", " ", text or "").strip()
    if not t:
        raise ValueError(f"empty {what}")
    if len(t) > limit:
        raise ValueError(f"{what}: {limit} characters max")
    return t


# ---- draft sync ------------------------------------------------------------------------------
def save_draft(user_id, handout_id, code, cursor_line=None):
    """Upsert his editor text for a handout (the page autosaves it). No ui_event.
    Returns {"ok": True, "updated_at"}."""
    code = code or ""
    if len(code) > DRAFT_MAX:
        raise ValueError(f"draft: {DRAFT_MAX} characters max")
    try:
        cursor_line = int(cursor_line) if cursor_line not in (None, "") else None
    except (TypeError, ValueError):
        cursor_line = None
    with session_scope() as s:
        _handout(s, user_id, handout_id)
        d = _draft(s, handout_id)
        now = datetime.now()
        if d is None:
            d = CodeDraft(user_id=user_id, handout_id=handout_id)
            s.add(d)
        d.code, d.cursor_line, d.updated_at = code, cursor_line, now
        return {"ok": True, "updated_at": now.isoformat()}


def _last_submission(s, user_id, handout_id):
    rows = s.scalars(select(Event).where(Event.user_id == user_id, Event.kind == "submission")
                     .order_by(Event.id.desc()).limit(200))
    for e in rows:
        p = e.payload or {}
        if p.get("handout_id") == handout_id:
            return {"code": p.get("code") or "", "verdict": p.get("verdict"),
                    "at": e.created_at.isoformat()}
    return None


def read_code(user_id, handout_id):
    """{code, cursor_line, updated_at, last_submission: {code, verdict, at} | None}. code is
    his autosaved draft ("" when the page never saved one); cursor_line is 1-based."""
    with session_scope() as s:
        _handout(s, user_id, handout_id)
        d = _draft(s, handout_id)
        return {"code": d.code if d else "", "cursor_line": d.cursor_line if d else None,
                "updated_at": _iso(d.updated_at) if d else None,
                "last_submission": _last_submission(s, user_id, handout_id)}


# ---- stub ------------------------------------------------------------------------------------
def set_stub(user_id, handout_id, code, s=None):
    """Store the starter code on the handout; the page loads it when his draft is empty.
    Leak-guarded (comments and identifiers count). Emits `editor`. Returns {"ok": True}."""
    code = (code or "").replace("\r\n", "\n").rstrip() + "\n"
    if not code.strip():
        raise ValueError("empty stub")
    if len(code) > CODE_MAX:
        raise ValueError(f"stub: {CODE_MAX} characters max")
    if s is None:
        with session_scope() as s2:
            return set_stub(user_id, handout_id, code, s2)
    h = _handout(s, user_id, handout_id)
    _guard(s, h, code, own_code=False)
    h.stub = code
    _svc().emit(user_id, "editor", h.id, s)
    return {"ok": True}


def get_stub(user_id, handout_id):
    with session_scope() as s:
        h = s.get(Handout, handout_id)
        return h.stub if h is not None and h.user_id == user_id else None


# ---- annotations -----------------------------------------------------------------------------
def _ann_row(a):
    return {"id": a.id, "line": a.line, "kind": a.kind, "text": a.text,
            "lines": list(a.lines or []) if a.kind == "highlight" else None,
            "anchor": a.anchor, "created_at": _iso(a.created_at),
            "resolved_at": _iso(a.resolved_at), "reply": a.reply, "reply_at": _iso(a.reply_at)}


def _line_text(s, handout_id, line):
    d = _draft(s, handout_id)
    if d is None or not d.code:
        return None
    rows = d.code.split("\n")
    return rows[line - 1] if 1 <= line <= len(rows) else None


def _check_line(line):
    if isinstance(line, bool):
        raise ValueError("line must be an int >= 1")
    try:
        n = int(line)
    except (TypeError, ValueError):
        raise ValueError("line must be an int >= 1") from None
    if n < 1:
        raise ValueError("line must be an int >= 1")
    return n


def annotate(user_id, handout_id, line, kind, text):
    """A box under `line` (1-based) of his code. kind: question | followup | note. A new
    question/followup resolves any older open question/followup: one open box at a time.
    Leak-guarded. Emits `annotations`. Returns the row."""
    if kind not in ANNOTATION_KINDS or kind == "highlight":
        raise ValueError("kind: question | followup | note (use highlight() for lines)")
    line = _check_line(line)
    text = _one_line(text, TEXT_MAX, "annotation")
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        _guard(s, h, text)
        now = datetime.now()
        if kind in ("question", "followup"):
            s.execute(update(Annotation).where(
                Annotation.handout_id == h.id, Annotation.resolved_at.is_(None),
                Annotation.kind.in_(("question", "followup"))).values(resolved_at=now))
        a = Annotation(user_id=user_id, handout_id=h.id, line=line, kind=kind, text=text,
                       anchor=_line_text(s, h.id, line), created_at=now)
        s.add(a)
        s.flush()
        _svc().emit(user_id, "annotations", h.id, s)
        return _ann_row(a)


def highlight(user_id, handout_id, lines, why=""):
    """Shade `lines` (1-based) of his code, with `why` as a small chip. Leak-guarded.
    Emits `annotations`. Returns the row."""
    if isinstance(lines, (int, str)):
        lines = [lines]
    nums = sorted({_check_line(n) for n in (lines or [])})
    if not nums:
        raise ValueError("highlight needs at least one line")
    if len(nums) > 80:
        raise ValueError("highlight: 80 lines max")
    why = re.sub(r"\s+", " ", why or "").strip()
    if len(why) > TEXT_MAX:
        raise ValueError(f"why: {TEXT_MAX} characters max")
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        if why:
            _guard(s, h, why)
        a = Annotation(user_id=user_id, handout_id=h.id, line=nums[0], kind="highlight",
                       text=why, lines=nums, anchor=_line_text(s, h.id, nums[0]))
        s.add(a)
        s.flush()
        _svc().emit(user_id, "annotations", h.id, s)
        return _ann_row(a)


def resolve_annotation(annotation_id, user_id=None):
    """Hide one box (resolved_at = now). user_id given: only his. Emits `annotations`.
    Returns {"ok": bool}."""
    with session_scope() as s:
        a = s.get(Annotation, annotation_id)
        if a is None or (user_id is not None and a.user_id != user_id):
            return {"ok": False, "error": "no such annotation"}
        if a.resolved_at is None:
            a.resolved_at = datetime.now()
        _svc().emit(a.user_id, "annotations", a.handout_id, s)
        return {"ok": True}


def clear_annotations(user_id, handout_id):
    """Resolve every open box and highlight of a handout. Emits `annotations`. Returns how many."""
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        n = s.execute(update(Annotation).where(
            Annotation.handout_id == h.id, Annotation.resolved_at.is_(None))
            .values(resolved_at=datetime.now())).rowcount
        _svc().emit(user_id, "annotations", h.id, s)
        return n or 0


def annotations(user_id, handout_id, include_resolved=False):
    """Open boxes of a handout (oldest first); include_resolved for the full record."""
    with session_scope() as s:
        q = select(Annotation).where(Annotation.user_id == user_id,
                                     Annotation.handout_id == handout_id)
        if not include_resolved:
            q = q.where(Annotation.resolved_at.is_(None))
        return [_ann_row(a) for a in s.scalars(q.order_by(Annotation.id))]


def reply_annotation(user_id, annotation_id, text):
    """His answer typed into a question/followup box: stored on the box, pushed to the coach
    as `annotation_reply` {id, handout_id, line, question, reply} and posted to the chat as
    "Re line N: <reply>". Emits `annotations`. Returns {"ok": True, "event_id"}."""
    text = re.sub(r"[ \t]+", " ", (text or "")).strip()
    if not text:
        raise ValueError("empty reply")
    if len(text) > 2000:
        raise ValueError("reply: 2000 characters max")
    with session_scope() as s:
        a = s.get(Annotation, annotation_id)
        if a is None or a.user_id != user_id:
            raise ValueError(f"no annotation {annotation_id}")
        if a.kind not in ("question", "followup"):
            raise ValueError("only a question or followup takes a reply")
        a.reply, a.reply_at = text, datetime.now()
        payload = {"id": a.id, "handout_id": a.handout_id, "line": a.line,
                   "question": a.text, "reply": text}
        hid, line = a.handout_id, a.line
        _svc().emit(user_id, "annotations", a.handout_id, s)
    eid = _svc().push_event(user_id, "annotation_reply", payload)
    _svc().post_message(user_id, "user", f"Re line {line}: {text}", handout_id=hid)
    return {"ok": True, "event_id": eid}


# ---- coach comments inserted into his code ---------------------------------------------------
def add_comment(user_id, handout_id, line, text):
    """Queue `# coach: <text>` for insertion above `line` (1-based) of his code; the page
    applies it with that line's indentation. Single line, 160 chars max, leak-guarded.
    Emits `code_ops`. Returns {"ok": True, "id"}."""
    line = _check_line(line)
    if "\n" in (text or "").strip():
        raise ValueError("comment: one line only")
    text = _one_line(text, COMMENT_MAX, "comment")
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        _guard(s, h, text)
        op = CodeOp(user_id=user_id, handout_id=h.id, op="insert_comment", line=line,
                    text=text)
        s.add(op)
        s.flush()
        _svc().emit(user_id, "code_ops", h.id, s)
        return {"ok": True, "id": op.id}


def pending_ops(user_id, handout_id):
    with session_scope() as s:
        rows = s.scalars(select(CodeOp).where(
            CodeOp.user_id == user_id, CodeOp.handout_id == handout_id,
            CodeOp.applied_at.is_(None)).order_by(CodeOp.id))
        return [{"id": o.id, "op": o.op, "line": o.line, "text": o.text} for o in rows]


def mark_op_applied(user_id, op_id):
    """The page applied an op. Atomic: {"ok", "first"}; first is False when another tab got
    there already (so the text is inserted once)."""
    with session_scope() as s:
        o = s.get(CodeOp, op_id)
        if o is None or o.user_id != user_id:
            return {"ok": False, "first": False}
        n = s.execute(update(CodeOp).where(CodeOp.id == op_id, CodeOp.applied_at.is_(None))
                      .values(applied_at=datetime.now())).rowcount
        return {"ok": True, "first": bool(n)}


# ---- scratch tabs ----------------------------------------------------------------------------
def scratch(user_id, handout_id, title, code):
    """A read-only tab beside solution.py with a teaching demo. Title and code are
    leak-guarded. Emits `scratch`. Returns {"ok": True, "id"}."""
    title = _one_line(title, TITLE_MAX, "scratch title")
    code = (code or "").replace("\r\n", "\n").rstrip()
    if not code:
        raise ValueError("empty scratch code")
    if len(code) > CODE_MAX:
        raise ValueError(f"scratch: {CODE_MAX} characters max")
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        _guard(s, h, title + "\n" + code)
        row = Scratch(user_id=user_id, handout_id=h.id, title=title, code=code)
        s.add(row)
        s.flush()
        _svc().emit(user_id, "scratch", h.id, s)
        return {"ok": True, "id": row.id}


def scratches(user_id, handout_id):
    with session_scope() as s:
        rows = s.scalars(select(Scratch).where(
            Scratch.user_id == user_id, Scratch.handout_id == handout_id).order_by(Scratch.id))
        return [{"id": r.id, "title": r.title, "code": r.code, "created_at": _iso(r.created_at)}
                for r in rows]


# ---- everything the page needs, in one read --------------------------------------------------
def editor_state(user_id, handout_id):
    """{handout_id, stub, draft: {code, cursor_line, updated_at} | None, annotations, ops,
    scratch} for the solve page."""
    with session_scope() as s:
        h = _handout(s, user_id, handout_id)
        d = _draft(s, h.id)
        stub = h.stub
    return {"handout_id": handout_id, "stub": stub,
            "draft": {"code": d.code, "cursor_line": d.cursor_line,
                      "updated_at": _iso(d.updated_at)} if d else None,
            "annotations": annotations(user_id, handout_id),
            "ops": pending_ops(user_id, handout_id),
            "scratch": scratches(user_id, handout_id)}


__all__ = ["save_draft", "read_code", "set_stub", "get_stub", "annotate", "highlight",
           "resolve_annotation", "clear_annotations", "annotations", "reply_annotation",
           "add_comment", "pending_ops", "mark_op_applied", "scratch", "scratches",
           "editor_state", "ANNOTATION_KINDS", "EDITOR_UI_KINDS"]
