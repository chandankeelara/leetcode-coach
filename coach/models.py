"""Tables. Plain SQLAlchemy 2 declarative, sqlite. `coach.db.init()` creates them."""
from datetime import date, datetime

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String,
                        Text, UniqueConstraint, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now():
    return datetime.now()


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))     # argon2; "!" = no password yet
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    # Coach presence heartbeat (UTC iso): written by every authenticated coach call and every
    # wait_event poll for THIS user. See service.coach_seen / coach_online.
    coach_last_seen: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # May open /feedback (the coach_requests list). db.init sets it for the owner account.
    is_admin: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class Plan(Base):
    """A learner's study plan: what the coach picks problems by. One active row per user
    (older rows stay, active=False). phases: [{name, start, end, rules}] (iso dates)."""
    __tablename__ = "plans"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200), default="My plan")
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    daily_problems: Mapped[int] = mapped_column(Integer, default=2)
    minutes_per_day: Mapped[int] = mapped_column(Integer, default=90)
    focus_patterns: Mapped[list] = mapped_column(JSON, default=list)     # [slug, ...] in order
    phases: Mapped[list] = mapped_column(JSON, default=list)
    hards_from: Mapped[date | None] = mapped_column(Date, nullable=True)  # None = no Hards
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Pattern(Base):
    __tablename__ = "patterns"
    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    tier: Mapped[int] = mapped_column(Integer)                  # 1, 2 or 3


class Problem(Base):
    __tablename__ = "problems"
    number: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    lc_slug: Mapped[str] = mapped_column(String(200))
    difficulty: Mapped[str] = mapped_column(String(1))          # E | M | H  (never shown to him)
    pattern_slug: Mapped[str | None] = mapped_column(ForeignKey("patterns.slug"), nullable=True)
    paraphrase: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16))             # bank | reproduction | attempt
    bank_group: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bank_order: Mapped[int | None] = mapped_column(Integer, nullable=True)   # position in the pool
    reproduction: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-problem hardness (coach/rating.py): set ONCE by the coach (rate_problem), clamped to
    # the band of `difficulty`. NULL = the band default. Never shown to him before he logs.
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CoachSession(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    day: Mapped[date] = mapped_column(Date)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    label: Mapped[str | None] = mapped_column(String(200), nullable=True)   # e.g. file stem
    log_md: Mapped[str | None] = mapped_column(Text, nullable=True)        # sessions/*.md verbatim


class Handout(Base):
    """A problem given out. `pattern_slug` and `slot` are INTERNAL, never in the public DTO."""
    __tablename__ = "handouts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    problem_number: Mapped[int] = mapped_column(ForeignKey("problems.number"))
    slot: Mapped[str] = mapped_column(String(16))   # warmup|drill|growth|retry|refire|imported
    pattern_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    handed_at: Mapped[datetime] = mapped_column(DateTime, default=_now)   # server clock
    target_min: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="open")       # open|logged|skipped
    editor_open: Mapped[bool | None] = mapped_column(Boolean, default=False)  # coach opens it
    # The coach's clock (coach/clock.py). NULLs on old rows = running since handed_at, accum 0.
    clock_state: Mapped[str | None] = mapped_column(String(8), nullable=True)  # running|paused
    clock_anchor: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # segment start
    clock_accum_s: Mapped[int | None] = mapped_column(Integer, nullable=True)  # banked seconds
    phase_label: Mapped[str | None] = mapped_column(String(40), nullable=True)
    first_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # never moves
    checkpoints_fired: Mapped[list | None] = mapped_column(JSON, nullable=True)   # [50, 100, ...]
    stub: Mapped[str | None] = mapped_column(Text, nullable=True)   # editor tools: starter code
    # Coach picks (service.hand_out): the mode it chose and its private one-line reason.
    # Both NULL on picker handouts (mode then follows the slot). pick_reason is shown only
    # after the attempt is logged (reveal, session_summary).
    mode: Mapped[str | None] = mapped_column(String(8), nullable=True)      # timed|coached
    pick_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Steps of a problem (coach/steps.py): {understand, approach, code} minutes; NULL = the old
    # single clock with a free phase label. The current step is phase_label. phase_elapsed:
    # {step: banked seconds}; phase_start_s: coach-clock seconds when the current step began.
    phase_budget: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    phase_elapsed: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    phase_start_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    phase_fired: Mapped[list | None] = mapped_column(JSON, nullable=True)   # steps past budget
    budget_reason: Mapped[str | None] = mapped_column(Text, nullable=True)   # private until logged
    idle_mark: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # idle fired for


class Attempt(Base):
    __tablename__ = "attempts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    problem_number: Mapped[int] = mapped_column(ForeignKey("problems.number"))
    pattern_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)  # as logged
    day: Mapped[date] = mapped_column(Date)
    handout_id: Mapped[int | None] = mapped_column(ForeignKey("handouts.id"), nullable=True)
    target_min: Mapped[int] = mapped_column(Integer)
    actual_min: Mapped[int] = mapped_column(Integer)
    bugs: Mapped[int] = mapped_column(Integer, default=0)
    hints: Mapped[int] = mapped_column(Integer, default=0)
    optimal: Mapped[bool] = mapped_column(Boolean, default=True)
    derived_cold: Mapped[bool] = mapped_column(Boolean, default=True)
    outcome: Mapped[str] = mapped_column(String(16))             # clean | solved | abandoned
    notes: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="app")   # import | app
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    wall_min: Mapped[int | None] = mapped_column(Integer, nullable=True)   # wall clock from start
    clock_note: Mapped[str | None] = mapped_column(Text, nullable=True)   # "2 pauses (...), 1 reset"

    # One attempt per handout (two coaches once both logged the same one). Partial: imported
    # rows have no handout. db.init adds it to an existing database when the data allows.
    __table_args__ = (Index("uq_attempts_handout", "handout_id", unique=True,
                            sqlite_where=text("handout_id IS NOT NULL"),
                            postgresql_where=text("handout_id IS NOT NULL")),)


class ClockEvent(Base):
    """Every coach clock action, with time and reason: the record shown on reveal and in the
    session summary. action: start | pause | resume | reset | target | phase."""
    __tablename__ = "clock_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    handout_id: Mapped[int] = mapped_column(ForeignKey("handouts.id"), index=True)
    action: Mapped[str] = mapped_column(String(8))
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    reason: Mapped[str] = mapped_column(Text, default="")
    value: Mapped[object] = mapped_column(JSON, default=dict)


class Skill(Base):
    __tablename__ = "skills"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(8))                 # pattern | meta
    pattern_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    level: Mapped[int] = mapped_column(Integer)                  # 0-5
    trend: Mapped[str] = mapped_column(String(64), default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    updated_on: Mapped[date | None] = mapped_column(Date, nullable=True)   # last set_skill day


class SkillEvent(Base):
    __tablename__ = "skill_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"))
    day: Mapped[date | None] = mapped_column(Date, nullable=True)
    level: Mapped[int] = mapped_column(Integer)
    evidence: Mapped[str] = mapped_column(Text, default="")


class RetryItem(Base):
    __tablename__ = "retry_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    problem_number: Mapped[int] = mapped_column(ForeignKey("problems.number"))
    added: Mapped[date] = mapped_column(Date)
    last_on: Mapped[date] = mapped_column(Date)      # date of the latest attempt (the ladder clock)
    reason: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    cleared_on: Mapped[date | None] = mapped_column(Date, nullable=True)


class PatternState(Base):
    __tablename__ = "pattern_state"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    pattern_slug: Mapped[str] = mapped_column(String(64))
    retired_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    refires: Mapped[list] = mapped_column(JSON, default=list)    # ["YYYY-MM-DD", ...]
    note_md: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("user_id", "pattern_slug"),)


class Burned(Base):
    """RULE ZERO leaks: a pattern named out loud is burned for that session."""
    __tablename__ = "burned"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    pattern_slug: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class OaSim(Base):
    __tablename__ = "oa_sims"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    day: Mapped[date] = mapped_column(Date)
    format: Mapped[str] = mapped_column(String(64), default="")
    problem: Mapped[str] = mapped_column(String(64), default="")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    read_all_first: Mapped[str] = mapped_column(String(8), default="")
    bailed_to_brute: Mapped[str] = mapped_column(String(8), default="")
    lookups: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class TrapRecall(Base):
    __tablename__ = "trap_recall"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    day: Mapped[date] = mapped_column(Date)
    pattern: Mapped[str] = mapped_column(String(64), default="")
    trap: Mapped[str] = mapped_column(Text, default="")
    cue: Mapped[str] = mapped_column(Text, default="")
    recalled: Mapped[str] = mapped_column(String(8), default="")
    hit_in_code: Mapped[str] = mapped_column(String(8), default="")


class Config(Base):
    __tablename__ = "config"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)


class Event(Base):
    """Queue: the web UI pushes, the bridge drains. `consumed` flips when drained."""
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[object] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class CoachMessage(Base):
    """Chat panel messages. role: user | coach | system."""
    __tablename__ = "coach_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    handout_id: Mapped[int | None] = mapped_column(ForeignKey("handouts.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    # a coach illustration posted as this message (the thread renders the visual's html)
    visual_id: Mapped[int | None] = mapped_column(ForeignKey("visuals.id"), nullable=True)


class Visual(Base):
    """Coach-drawn illustrations (array, grid, tree, ...), shown in the discussion thread as
    the coach message that carries `visual_id`.
    `html` is the rendered, escaped markup; `spec` the JSON it came from."""
    __tablename__ = "visuals"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    handout_id: Mapped[int] = mapped_column(ForeignKey("handouts.id"))
    kind: Mapped[str] = mapped_column(String(16))
    spec: Mapped[object] = mapped_column(JSON, default=dict)
    html: Mapped[str] = mapped_column(Text)
    caption: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # clear_visuals


class UiEvent(Base):
    """Change signal for open pages: every coach-side write appends one row; the web process
    streams them over SSE (/stream). user_id NULL = every user (presence). Rows older than an
    hour are pruned on each emit. kind: message | statement | visual | editor | handout |
    clock | presence."""
    __tablename__ = "ui_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    # AUTOINCREMENT: ids never reused after pruning, so a page's `since` stays valid.
    __table_args__ = {"sqlite_autoincrement": True}


# ---- editor tools (coach/editor.py) ---------------------------------------------------------
class CodeDraft(Base):
    """His editor text, autosaved by the page (one row per handout, upserted)."""
    __tablename__ = "code_drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    handout_id: Mapped[int] = mapped_column(ForeignKey("handouts.id"), unique=True)
    code: Mapped[str] = mapped_column(Text, default="")
    cursor_line: Mapped[int | None] = mapped_column(Integer, nullable=True)     # 1-based
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Annotation(Base):
    """A coach box under one line of his code. kind: question | followup | note | highlight.
    A highlight's lines live in `lines` (1-based); `anchor` is the line's text when placed."""
    __tablename__ = "annotations"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    handout_id: Mapped[int] = mapped_column(ForeignKey("handouts.id"), index=True)
    line: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text, default="")
    lines: Mapped[list | None] = mapped_column(JSON, nullable=True)
    anchor: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    reply_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class CodeOp(Base):
    """A pending edit the page applies into his editor. op: insert_comment."""
    __tablename__ = "code_ops"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    handout_id: Mapped[int] = mapped_column(ForeignKey("handouts.id"), index=True)
    op: Mapped[str] = mapped_column(String(32), default="insert_comment")
    line: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Scratch(Base):
    """A read-only code tab the coach opens beside solution.py (teaching demos)."""
    __tablename__ = "scratch"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    handout_id: Mapped[int] = mapped_column(ForeignKey("handouts.id"), index=True)
    title: Mapped[str] = mapped_column(String(80))
    code: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ---- tests panel: cases, spec and runs (coach/runner.py executes) ---------------------------
class TestSpec(Base):
    """How a handout's cases are called: mode function|design and the entry
    ({"class", "method"}). One row per handout, written by set_tests."""
    __tablename__ = "test_specs"
    __test__ = False          # not a pytest class
    handout_id: Mapped[int] = mapped_column(ForeignKey("handouts.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    mode: Mapped[str] = mapped_column(String(16), default="function")
    entry: Mapped[object] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class TestCase(Base):
    """One test case of a handout. source: coach (set_tests, replaced as a set) | user."""
    __tablename__ = "test_cases"
    __test__ = False
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    handout_id: Mapped[int] = mapped_column(ForeignKey("handouts.id"), index=True)
    source: Mapped[str] = mapped_column(String(8), default="coach")
    case: Mapped[object] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class TestRun(Base):
    """One run of the cases. who: user (Run button) | coach (run_tests, never shown to him)."""
    __tablename__ = "test_runs"
    __test__ = False
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    handout_id: Mapped[int] = mapped_column(ForeignKey("handouts.id"), index=True)
    who: Mapped[str] = mapped_column(String(8), default="user")
    code: Mapped[str] = mapped_column(Text, default="")
    results: Mapped[object] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Submission(Base):
    """What he submitted (the code of a `submission` event) and how the coach judged it.
    verdict: Accepted | Wrong Answer | Time Limit Exceeded | Runtime Error | None (not judged)."""
    __tablename__ = "submissions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    handout_id: Mapped[int] = mapped_column(ForeignKey("handouts.id"), index=True)
    code: Mapped[str] = mapped_column(Text, default="")
    verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)
    judged: Mapped[object] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ---- rating (coach/rating.py) ---------------------------------------------------------------
class Rating(Base):
    """A learner's MMR. One row per user; created by placement (replay of his attempts)."""
    __tablename__ = "ratings"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    mmr: Mapped[float] = mapped_column(Float, default=1200.0)
    games: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class RatingEvent(Base):
    """One rated attempt (a "match" against the problem's rating). attempt_id unique: an
    attempt changes the rating once."""
    __tablename__ = "rating_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("attempts.id"), unique=True)
    before: Mapped[float] = mapped_column(Float)
    after: Mapped[float] = mapped_column(Float)
    delta: Mapped[float] = mapped_column(Float)
    expected: Mapped[float] = mapped_column(Float)
    actual: Mapped[float] = mapped_column(Float)
    k: Mapped[int] = mapped_column(Integer)
    problem_rating: Mapped[int] = mapped_column(Integer)
    breakdown: Mapped[object] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ---- feedback to the dev team (coach/feedback.py) -------------------------------------------
class CoachRequest(Base):
    """What the coach (model) asked the dev team for, or a tool that failed it. kind: feature |
    friction | bug. severity: blocked | workaround | nice. status: open | planned | done.
    Deduped by key (normalised title + tool): repeats bump count / last_at. No student code."""
    __tablename__ = "coach_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handout_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(16))
    key: Mapped[str] = mapped_column(String(300), index=True)
    title: Mapped[str] = mapped_column(String(200))
    why: Mapped[str] = mapped_column(Text, default="")
    example: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(16), default="nice")
    tool: Mapped[str | None] = mapped_column(String(64), nullable=True)
    count: Mapped[int] = mapped_column(Integer, default=1)
    first_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    status: Mapped[str] = mapped_column(String(16), default="open")


class CoachToken(Base):
    """Bearer tokens for the website's coach tools (/mcp and /tools). Only the sha256 of the
    token is stored; the token itself is shown once, when created."""
    __tablename__ = "coach_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
