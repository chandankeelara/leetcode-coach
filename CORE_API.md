# CORE_API

What the web UI and the MCP bridge call. Everything lives in `coach/service.py` unless noted.
Each function opens its own DB session and commits. DB path: env `COACH_DB`
(default `./coach.db`); call `coach.db.init(path)` once at start-up (tests pass a tmp path).
`today` is an optional keyword (a `date`) on date-sensitive calls; omit it in production.

## RULE ZERO

Only `next_problem()` and `reveal()` carry problem data, and both are safe:

* `next_problem()` returns the `Handoff` DTO. It has no pattern, slot, tier, difficulty or
  progress field (`extra="forbid"`, a test pins the field set).
* `reveal()` raises `RevealBlocked` until the attempt for that handout is logged.
* `status()` and `report()` are PROGRESS talk. Never render them on the same screen as a
  handoff, nor in the message just before one. The UI shows them on `/dashboard` only after
  logging; the bridge returns them only in a message with no problem in it.

## Handoff (`coach.picker.Handoff`, pydantic v2)

| field | type | note |
|---|---|---|
| `handout_id` | int | pass back to `log_attempt` / `reveal` / `skip_problem` |
| `number` | int | LeetCode number |
| `title` | str | |
| `leetcode_url` | str | `https://leetcode.com/problems/<slug>/` |
| `paraphrase` | str or None | his own words, if stored |
| `started_at` | datetime | server clock, stamped at handout; he never types it |
| `target_min` | int | |
| `instruction` | str | always "Give your approach in plain words before any code." |

Nothing else. `h.model_dump()` / `h.model_dump_json()` are safe to send to a browser.

## Signatures

```python
# users
create_user(username: str, password: str) -> int            # user_id; ValueError if it exists.
                                                            # A migrated owner has no password yet
                                                            # ("!") and gets one from this call.
set_password(username: str, password: str) -> None
verify_user(username: str, password: str) -> int | None     # user_id, or None on any failure
default_user_id() -> int | None                             # the first (only) user

# sessions
start_session(user_id: int, today: date | None = None, label: str | None = None) -> int  # session_id
end_session(session_id: int, log_md: str | None = None) -> int

# the handoff
next_problem(user_id: int, today: date | None = None, force_new: bool = False) -> Handoff | None
#   an open handout from today is returned again (same id and start time);
#   force_new=True marks it skipped and picks again. None = nothing passes the filters.
skip_problem(handout_id: int) -> None                       # status -> skipped; still blocks 7 days

log_attempt(user_id: int, handout_id: int | None = None, number: int | None = None,
            actual_min: int = 0, bugs: int = 0, optimal: bool = True, derived_cold: bool = True,
            hints: int = 0, notes: str = "", abandoned: bool = False,
            target_min: int | None = None, day: date | None = None) -> dict
#   -> {"attempt_id", "outcome": clean|solved|abandoned, "retry": added|bumped|cleared|none,
#       "problem_number"}
#   outcome comes from rules.outcome (0 bugs, <=1.5x target, optimal, derived cold; hints > 0
#   counts as not derived cold). Retry queue: added when over 1.5x, 2+ bugs, a hint, suboptimal,
#   not cold, or abandoned; an already-queued item gets attempts+1; clean clears it.
#   Third clean solve of a pattern writes pattern_state.retired_on; finishing a "refire" handout
#   appends to its refires.

reveal(handout_id: int) -> dict                             # raises RevealBlocked until logged
#   -> {"handout_id","number","title","slot","pattern","pattern_name","tier"}
burn_pattern(user_id: int, slug: str, reason: str = "", today: date | None = None) -> int
#   RULE ZERO leak: pattern is out of play for the current session (the picker honours it)

set_statement(user_id: int, handout_id: int, text: str) -> dict   # {"ok", "handout_id", "number"}
#   coach-written statement, stored in problems.paraphrase (overwrites), returned in Handoff.paraphrase.
#   LEAK GUARD: raises ValueError("leak: word, word") and stores nothing if text names a pattern
#   (patterns table names/slugs + synonyms such as sliding window, heap, dp, greedy) or easy/medium/
#   hard. Case-, hyphen- and plural-insensitive. A term the problem's own title contains is allowed.
get_statement(handout_id: int) -> str | None

# progress (see RULE ZERO)
status(user_id: int, today: date | None = None) -> dict
#   {"today", "backlog": {backlog,target_today,done_today,remaining,days,owed_before,done_before,start},
#    "streak": {current,longest,last14},
#    "tiers": {tier1:[{pattern,tier,clean,needed,tried,status}], tier2:[...], owed, clean_rate,
#              tier2_open, attempts_needed, ready_1_per_day, ready_at_55},
#    "retry": [{problem_number,title,attempts,age,wait,due,last_on,reason}],
#    "refire": [{pattern,age,due,refires_done,last_touch}],
#    "blocklist": [numbers], "weakest": [5 x skill], "strongest": [skill, level 4+]}
report(user_id: int, target_date: date | str | None = None, today: date | None = None) -> dict
#   report.py as data: {"throughput","quality","coverage","projection", ...}; {} with no attempts.

# queue and chat
push_event(user_id: int, kind: str, payload: dict | None = None) -> int          # event id
async wait_event(user_id: int, timeout_s: float = 25.0, poll_s: float = 0.25) -> list[dict]
#   long-poll: returns and marks consumed ALL pending events [{"id","kind","payload","created_at"}],
#   or [] on timeout. Single consumer assumed (the bridge).
post_message(user_id: int, role: str, content: str, handout_id: int | None = None) -> int
list_messages(user_id: int, since_id: int = 0, limit: int = 200) -> list[dict]
#   [{"id","role","content","handout_id","created_at"}]  role: user | coach | system
```

Exception: `service.RevealBlocked`.

## Other modules the builders may use

* `coach.db`: `init(path=None)`, `session_scope()` (context manager), `new_session()`.
* `coach.models`: all tables (users, patterns, problems, attempts, handouts, sessions, skills,
  skill_events, retry_items, pattern_state, burned, oa_sims, trap_recall, config, events,
  coach_messages).
* `coach.lint.lint(src) -> [(line, kind, msg)]`, kinds USELESS, NONE-ASSIGN, COST, ALIAS, SYNTAX.
  Not wired into `log_attempt`; the caller runs it on the pasted code.
* `coach.rules`: the pure functions (`backlog`, `streak`, `tier_progress`, `retry_due`,
  `refire_due`, `blocklist`, `outcome`, `report_card`, ...), taking rows + `today`.
* `coach.picker.pick_next(session, user_id, today)` is the lower level; prefer `next_problem`.

## CLI

```
python -m coach.cli create-user NAME     # getpass, or env COACH_PASSWORD
python -m coach.cli migrate [OLD_REPO] [--force]
python -m coach.cli verify
python -m coach.cli status [--json]
python -m coach.cli next [--json] [--new]
python -m coach.cli log --handout ID --min N [--bugs N] [--hints N] [--no-optimal] [--no-cold] [--abandoned] [--notes TEXT]
python -m coach.cli reveal HANDOUT_ID
```

## Notes for the builders

* Slot order per session is warm-up, drill, growth, retry, cycling by the number of handouts in
  the open session; a slot with no candidate falls through to the next.
* A session is opened lazily (`next_problem`, `burn_pattern`, `start_session`) for `today`;
  burned patterns are per session. Call `end_session` to close one.
* Imported history has `attempts.source = "import"`; new rows are `"app"`. Re-running the
  migration refuses once `"app"` attempts exist unless `--force`.
* The migrated owner user is `chandan` with no password: run `create-user chandan` to set it.
