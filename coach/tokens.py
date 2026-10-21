"""Coach tokens: what a student's Claude Code sends as `Authorization: Bearer <token>` to the
website's coach tools (/mcp and /tools). Only sha256(token) is stored; the token is returned
once, by create(). A token is the user: every tool call runs as its user_id."""
import hashlib
import secrets
from datetime import datetime

from sqlalchemy import select

from .db import session_scope
from .models import CoachToken, User

PREFIX = "coach_"
LAST_USED_EVERY_S = 60          # last_used_at write throttle


def _hash(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create(user_id, label=""):
    """New token for user_id. Returns {"id", "token", "label", "created_at"}; the token is
    never retrievable again."""
    raw = PREFIX + secrets.token_urlsafe(32)
    with session_scope() as s:
        if s.get(User, user_id) is None:
            raise ValueError(f"no user {user_id}")
        t = CoachToken(user_id=user_id, token_hash=_hash(raw), label=(label or "").strip()[:80])
        s.add(t)
        s.flush()
        return {"id": t.id, "token": raw, "label": t.label, "created_at": t.created_at.isoformat()}


def verify(raw, now=None):
    """user_id for a live token, else None. Updates last_used_at (at most once a minute)."""
    if not raw or not isinstance(raw, str):
        return None
    now = now or datetime.now()
    with session_scope() as s:
        t = s.scalars(select(CoachToken).where(CoachToken.token_hash == _hash(raw.strip()))).first()
        if t is None or t.revoked_at is not None:
            return None
        if t.last_used_at is None or (now - t.last_used_at).total_seconds() >= LAST_USED_EVERY_S:
            t.last_used_at = now
        return t.user_id


def bearer(header_value):
    """The token in an `Authorization: Bearer <token>` header value, or None."""
    if not header_value:
        return None
    scheme, _, tok = header_value.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return tok.strip() or None


def list_for(user_id):
    """This user's tokens, newest first: [{id, label, created_at, last_used_at, revoked_at}]."""
    with session_scope() as s:
        rows = s.scalars(select(CoachToken).where(CoachToken.user_id == user_id)
                         .order_by(CoachToken.id.desc()))
        return [{"id": t.id, "label": t.label, "created_at": t.created_at,
                 "last_used_at": t.last_used_at, "revoked_at": t.revoked_at} for t in rows]


def revoke(user_id, token_id):
    """Revoke one of THIS user's tokens. True if it was live, False if already revoked;
    ValueError if it is not theirs."""
    with session_scope() as s:
        t = s.get(CoachToken, token_id)
        if t is None or t.user_id != user_id:
            raise ValueError(f"no token {token_id}")
        if t.revoked_at is not None:
            return False
        t.revoked_at = datetime.now()
        return True


def user_id_by_name(username):
    with session_scope() as s:
        return s.scalars(select(User.id).where(User.username == username)).first()
