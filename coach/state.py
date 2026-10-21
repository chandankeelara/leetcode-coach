"""Small shared helpers over the database: config, the current session, row loaders."""
from datetime import date, datetime

from sqlalchemy import select

from . import rules
from .models import (Attempt, CoachSession, Config, Handout, PatternState, Problem, RetryItem,
                     Skill)

DEFAULT_CONFIG = {
    "backlog_start": rules.BACKLOG_START.isoformat(),
    "per_day": rules.PROBLEMS_PER_DAY,
    "ladder": rules.LADDER,
    "clean_to_own": rules.CLEAN_TO_OWN,
    "hards_from": rules.HARDS_FROM.isoformat(),
    "daily_min": rules.DAILY_MIN,
    "target_min": rules.TARGET_MIN,
    "block_days": rules.BLOCK_DAYS,
    "refire_days": rules.REFIRE_DAYS,
    "tier1": rules.TIER1,
    "tier2": rules.TIER2,
}


def load_config(s):
    """Config table over the defaults. Dates come back as `date` objects."""
    cfg = dict(DEFAULT_CONFIG)
    for row in s.scalars(select(Config)):
        cfg[row.key] = row.value
    cfg["backlog_start"] = date.fromisoformat(cfg["backlog_start"])
    cfg["hards_from"] = date.fromisoformat(cfg["hards_from"])
    return cfg


def current_session(s, user_id, today, create=True):
    """The open session for `today` (ended_at is null), optionally creating one."""
    row = s.scalars(
        select(CoachSession)
        .where(CoachSession.user_id == user_id, CoachSession.day == today,
               CoachSession.ended_at.is_(None))
        .order_by(CoachSession.id.desc())
    ).first()
    if row is None and create:
        row = CoachSession(user_id=user_id, day=today,
                           started_at=datetime.combine(today, datetime.now().time()))
        s.add(row)
        s.flush()
    return row


def user_attempts(s, user_id):
    return list(s.scalars(select(Attempt).where(Attempt.user_id == user_id)
                          .order_by(Attempt.day, Attempt.id)))


def user_handouts(s, user_id):
    return list(s.scalars(select(Handout).where(Handout.user_id == user_id)))


def user_retry_items(s, user_id, open_only=True):
    q = select(RetryItem).where(RetryItem.user_id == user_id)
    if open_only:
        q = q.where(RetryItem.cleared_on.is_(None))
    return list(s.scalars(q.order_by(RetryItem.id)))


def user_pattern_state(s, user_id):
    return list(s.scalars(select(PatternState).where(PatternState.user_id == user_id)))


def user_skills(s, user_id):
    return list(s.scalars(select(Skill).where(Skill.user_id == user_id)))


def all_problems(s):
    return {p.number: p for p in s.scalars(select(Problem))}
