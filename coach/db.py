"""Engine and session handling.

Which database, in order: the `path` argument, env COACH_DB_URL (any SQLAlchemy URL, e.g.
postgresql+psycopg://user:pw@host/db), env COACH_DB (sqlite file path), ./coach.db.
"""
import os
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
    _maker = sessionmaker(_engine, expire_on_commit=False, future=True)
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
