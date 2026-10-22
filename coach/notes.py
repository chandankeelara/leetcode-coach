"""Learner notes: what the coach learned about how HE learns (which explanations land, pace,
motivation). Read before explaining; add one when something clearly landed or clearly failed.

    add_note(user_id, kind, note)        -> {id, kind, note, created_at, active}
    list_notes(user_id, active_only=True) -> [same]
    retire_note(user_id, note_id)        -> {ok, id}       (active = False; kept for history)

kind: explanation | motivation | pace | other. note: at most 300 characters, no code, not his
name. `get_profile` returns the active ones as `notes`. `seed(s)` (db.init) gives the owner
account its first note once.
"""
import re
from datetime import datetime

from sqlalchemy import select

from .db import session_scope
from .models import LearnerNote, User

KINDS = ("explanation", "motivation", "pace", "other")
NOTE_MAX = 300
SEED_USER = "chandan"
SEED_NOTE = ("explanation", "Animations for relinking (143) didn't help; plain before/after "
                            "stills and plain-words restatements did.")


def _row(n):
    return {"id": n.id, "kind": n.kind, "note": n.note,
            "created_at": n.created_at.isoformat() if n.created_at else None,
            "active": bool(n.active)}


def _check(s, user_id, kind, note):
    from .feedback import looks_like_code
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    raw = str(note or "")
    if looks_like_code(raw) or re.search(r"\bdef \w+\(|\bclass \w+.*:|\w+\.next\s*=", raw):
        raise ValueError("note: plain words, no code")
    note = " ".join(raw.split())
    if not note:
        raise ValueError("empty note")
    if len(note) > NOTE_MAX:
        raise ValueError(f"note: {NOTE_MAX} characters max")
    u = s.get(User, user_id)
    if u is not None and u.username and u.username.lower() in note.lower():
        raise ValueError("note: no names")
    return note


def add_note(user_id, kind, note):
    with session_scope() as s:
        note = _check(s, user_id, kind, note)
        n = LearnerNote(user_id=user_id, kind=kind, note=note, created_at=datetime.now(),
                        active=True)
        s.add(n)
        s.flush()
        return _row(n)


def notes_for(s, user_id, active_only=True):
    q = select(LearnerNote).where(LearnerNote.user_id == user_id)
    if active_only:
        q = q.where(LearnerNote.active.is_(True))
    return [_row(n) for n in s.scalars(q.order_by(LearnerNote.id))]


def list_notes(user_id, active_only=True):
    with session_scope() as s:
        return notes_for(s, user_id, active_only)


def retire_note(user_id, note_id):
    with session_scope() as s:
        n = s.get(LearnerNote, note_id)
        if n is None or n.user_id != user_id:
            raise ValueError(f"no note {note_id}")
        n.active = False
        return {"ok": True, "id": n.id}


def seed(s):
    """The owner's first note (2026-10-22 evidence), once: only when he has no notes at all."""
    u = s.scalars(select(User).where(User.username == SEED_USER)).first()
    if u is None:
        return False
    if s.scalars(select(LearnerNote.id).where(LearnerNote.user_id == u.id)).first() is not None:
        return False
    s.add(LearnerNote(user_id=u.id, kind=SEED_NOTE[0], note=SEED_NOTE[1],
                      created_at=datetime.now(), active=True))
    return True
