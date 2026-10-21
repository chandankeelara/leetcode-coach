"""Engine and session handling. One sqlite file; path from env COACH_DB (default ./coach.db)."""
import os
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .models import Base

_engine = None
_maker = None


def _url(path):
    if "://" in path:
        return path
    return "sqlite:///" + os.path.abspath(path).replace("\\", "/")


def init(path=None):
    """(Re)point the module at a database and create any missing tables. Returns the engine."""
    global _engine, _maker
    path = path or os.environ.get("COACH_DB") or "./coach.db"
    _engine = create_engine(_url(path), future=True)

    @event.listens_for(_engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(_engine)
    _maker = sessionmaker(_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine():
    if _engine is None:
        init()
    return _engine


def new_session():
    if _maker is None:
        init()
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
