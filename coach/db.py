"""Engine and session handling.

Which database, in order: the `path` argument, env COACH_DB_URL (any SQLAlchemy URL, e.g.
postgresql+psycopg://user:pw@host/db), env COACH_DB (sqlite file path), ./coach.db.
"""
import os
import threading
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from .models import Base

_engine = None
_maker = None


def _url(path):
    if "://" in path:
        if path.startswith("postgres://"):      # some hosts hand out the old scheme
            path = "postgresql://" + path[len("postgres://"):]
        return path
    return "sqlite:///" + os.path.abspath(path).replace("\\", "/")


def init(path=None):
    """(Re)point the module at a database and create any missing tables. Returns the engine."""
    global _engine, _maker
    path = (path or os.environ.get("COACH_DB_URL") or os.environ.get("COACH_DB")
            or "./coach.db")
    url = _url(path)
    _engine = create_engine(url, future=True, pool_pre_ping=not url.startswith("sqlite"))

    if url.startswith("sqlite"):
        @event.listens_for(_engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    Base.metadata.create_all(_engine)
    add_missing_columns(_engine)
    add_attempt_handout_index(_engine)
    _maker = sessionmaker(_engine, expire_on_commit=False, future=True)
    # Data migration: users with history from before plans get PLAN.md as their plan.
    from . import plans
    with session_scope() as s:
        plans.migrate_legacy(s)
    # Data migration: placement MMR for users with history (coach/rating.py), and the owner
    # account is admin (/feedback) unless set otherwise.
    from . import rating
    with session_scope() as s:
        rating.migrate(s)
        s.execute(text("UPDATE users SET is_admin = :t WHERE username = :u AND is_admin IS NULL"),
                  {"t": True, "u": os.environ.get("COACH_ADMIN", "chandan")})
    return _engine


def add_missing_columns(engine):
    """Additive migration: `create_all` never alters an existing table, so add every model
    column the database lacks (nullable, no constraint). Returns ["table.column", ...] added.
    Only safe for additive, nullable columns, which is the only kind of schema change allowed."""
    insp = inspect(engine)
    have_tables = set(insp.get_table_names())
    added = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in have_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))
                added.append(f"{table.name}.{col.name}")
    return added


def add_attempt_handout_index(engine):
    """Partial unique index attempts(handout_id) WHERE handout_id IS NOT NULL, for a database
    created before it existed. If duplicates are already there the index is skipped (logged) and
    service.log_attempt still refuses a second log in code. Returns True when the index exists."""
    import sys
    try:
        with engine.begin() as conn:
            conn.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_attempts_handout '
                              'ON attempts (handout_id) WHERE handout_id IS NOT NULL'))
        return True
    except Exception as e:                      # duplicates in live data: enforce in code only
        print(f"[coach.db] uq_attempts_handout not created ({type(e).__name__}); "
              "duplicate attempts per handout exist, log_attempt still refuses new ones",
              file=sys.stderr)
        return False


_init_lock = threading.Lock()


def _ensure_init():
    # The first requests after a start can arrive together (page + live stream); without the
    # lock each one runs init() and the data migrations race (seen 2026-10-21: two placement
    # inserts, UNIQUE constraint failed on ratings.user_id).
    if _maker is None:
        with _init_lock:
            if _maker is None:
                init()


def get_engine():
    _ensure_init()
    return _engine


def new_session():
    _ensure_init()
    return _maker()


@contextmanager
def session_scope():
    """Commit on success, roll back on error."""
    s = new_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
