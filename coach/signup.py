"""Self-service signup: validation, invite codes, the signup mode and a per-IP rate limit.

    COACH_SIGNUP = invite (default) | open | closed

* invite: the form needs a code from the `invites` table (admin page /admin/invites, or
  `python -m coach.cli invite`). A code works once: it is claimed in the same transaction that
  creates the user, so two people racing one code cannot both get in.
* open: anyone. closed: the page says so and POST is refused.
* No email is sent. The address is stored (unique, lowercase) for the day verification and
  password reset come with hosting.
"""
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .db import session_scope
from .models import Invite, User

MODES = ("invite", "open", "closed")
USERNAME_RE = re.compile(r"^[a-z0-9_]{3,24}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
PASSWORD_MIN = 10
RATE_LIMIT, RATE_WINDOW_S = 5, 3600
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"      # no 0/O/1/I


class SignupRefused(ValueError):
    """Bad form input. str(e) is the message to show; e.field says which field."""

    def __init__(self, message, field=None):
        super().__init__(message)
        self.field = field


def mode():
    m = (os.environ.get("COACH_SIGNUP") or "invite").strip().lower()
    return m if m in MODES else "invite"


# ---- rate limit (in memory, per process: signups are rare, a restart forgives) ----------------
_hits = defaultdict(deque)
_lock = threading.Lock()


def rate_limited(ip, now=None):
    """Count one signup POST from `ip`; True when it is over 5 in the last hour (the POST is
    then refused and not counted again)."""
    now = time.monotonic() if now is None else now
    with _lock:
        q = _hits[ip or "?"]
        while q and now - q[0] >= RATE_WINDOW_S:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return True
        q.append(now)
        return False


def reset_limits():
    with _lock:
        _hits.clear()


# ---- validation ---------------------------------------------------------------------------
def check_username(v):
    v = (v or "").strip()
    if not USERNAME_RE.match(v):
        raise SignupRefused("Username: 3 to 24 characters, lowercase letters, digits and "
                            "underscore only.", "username")
    return v


def check_email(v):
    v = (v or "").strip().lower()
    if len(v) > 254 or not EMAIL_RE.match(v):
        raise SignupRefused("That does not look like an email address.", "email")
    return v


def check_password(pw, username="", confirm=None):
    pw = pw or ""
    if len(pw) < PASSWORD_MIN:
        raise SignupRefused(f"Password: at least {PASSWORD_MIN} characters.", "password")
    classes = sum(bool(re.search(p, pw)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^a-zA-Z0-9]"))
    if classes < 2:
        raise SignupRefused("Password: mix at least two of lowercase, uppercase, digits and "
                            "symbols.", "password")
    if username and pw.lower() == username.lower():
        raise SignupRefused("Password: not the same as the username.", "password")
    if confirm is not None and pw != confirm:
        raise SignupRefused("The two passwords differ.", "confirm")
    return pw


def norm_code(v):
    return re.sub(r"[\s-]+", "", (v or "")).upper()


def _fmt(code):
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


# ---- invites --------------------------------------------------------------------------------
def create_invite(created_by=None, note=""):
    """New one-use code, stored as 12 characters (shown XXXX-XXXX-XXXX). Returns it as shown."""
    note = " ".join(str(note or "").split())[:200]
    with session_scope() as s:
        for _ in range(5):
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
            if s.get(Invite, code) is None:
                s.add(Invite(code=code, created_by=created_by, note=note))
                return _fmt(code)
    raise RuntimeError("could not make an invite code")


def revoke_invite(code):
    """True when the code existed and was still usable (a used code stays used)."""
    with session_scope() as s:
        r = s.execute(update(Invite).where(
            Invite.code == norm_code(code), Invite.used_by.is_(None),
            Invite.revoked_at.is_(None)).values(revoked_at=datetime.now()))
        return r.rowcount == 1


def list_invites():
    """Newest first: [{code, status: unused|used|revoked, note, created_at, created_by_name,
    used_at, used_by_name}]."""
    with session_scope() as s:
        names = {u.id: u.username for u in s.scalars(select(User))}
        rows = s.scalars(select(Invite).order_by(Invite.created_at.desc(), Invite.code)).all()
        return [{"code": _fmt(i.code), "raw": i.code, "note": i.note or "",
                 "status": "used" if i.used_by else ("revoked" if i.revoked_at else "unused"),
                 "created_at": i.created_at, "created_by_name": names.get(i.created_by),
                 "used_at": i.used_at, "used_by_name": names.get(i.used_by)} for i in rows]


# ---- signup ---------------------------------------------------------------------------------
def signup(username, email, password, confirm, invite_code="", today=None):
    """Validate, claim the invite (invite mode) and create the user in one transaction.
    Returns the new user_id. SignupRefused with a plain message otherwise."""
    from .service import _hasher
    m = mode()
    if m == "closed":
        raise SignupRefused("Signups are closed.")
    username = check_username(username)
    email = check_email(email)
    check_password(password, username, confirm)
    code = norm_code(invite_code)
    if m == "invite" and not code:
        raise SignupRefused("An invite code is needed to sign up.", "invite")
    pw_hash = _hasher.hash(password)
    try:
        with session_scope() as s:
            if s.scalars(select(User.id).where(User.username == username)).first():
                raise SignupRefused("That username is taken.", "username")
            if s.scalars(select(User.id).where(User.email == email)).first():
                raise SignupRefused("That email already has an account.", "email")
            u = User(username=username, password_hash=pw_hash, email=email)
            s.add(u)
            s.flush()
            if m == "invite":
                r = s.execute(update(Invite).where(
                    Invite.code == code, Invite.used_by.is_(None),
                    Invite.revoked_at.is_(None)).values(used_by=u.id, used_at=datetime.now()))
                if r.rowcount != 1:
                    raise SignupRefused("That invite code is not valid, or was already used.",
                                        "invite")
            return u.id
    except IntegrityError:
        raise SignupRefused("That username or email is already taken.", "username") from None
