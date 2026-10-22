# CORE_API

What the web UI and the MCP bridge call. Everything lives in `coach/service.py` unless noted.
Each function opens its own DB session and commits. DB path: env `COACH_DB`
(default `./coach.db`); call `coach.db.init(path)` once at start-up (tests pass a tmp path).
`today` is an optional keyword (a `date`) on date-sensitive calls; omit it in production.

## RULE ZERO

The coach chooses each problem (`hand_out`), so the coach knows the pattern; the learner never
sees it before logging:

* `hand_out()` and `next_problem()` return the `Handoff` DTO. It has no pattern, slot, tier,
  difficulty or progress field (`extra="forbid"`, a test pins the field set). The coach's
  `why` is stored privately (`handouts.pick_reason`) and only comes back after logging.
* Every coach-authored text is refused with `ValueError("leak: names the open problem's
  pattern")` while it names the OPEN problem's pattern (name, slug, synonyms:
  `service._PATTERN_SYNONYMS`) and he has not named it himself (his chat, approach, chat /
  submission events, code draft, box replies for that handout). Covers `post_message(role=
  "coach")` (so the bridge's `say`), `set_statement`, `show_visual`, `clock(phase)`, the editor
  tools (annotate, highlight, add_comment, scratch, set_stub) and `set_tests` labels. Only
  while the handout is open: after `log_attempt` the post-mortem may name it. A different
  pattern's name is not this guard's business (the older `leak_words` guard on statements,
  visuals, boxes and labels still refuses every technique word there).
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
| `mode` | `"timed"` or `"coached"` | how the coach behaves; slot-neutral (below) |

Nothing else (9 keys; the bridge refuses any other set, and any other mode value).

`mode` is derived from the internal slot by `picker.mode_for`, and each value covers several
slots, so it never names a slot, pattern, tier or difficulty:

* `timed` = warm-up / strength keeper and re-fire. The coach answers only direct questions,
  no prompting, until he submits.
* `coached` = drill, growth, retry. Full Socratic coaching. `h.model_dump()` / `h.model_dump_json()` are safe to send to a browser.

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

# plans (coach/plans.py): one active plan per user; the coach picks by it
get_plan(user_id, today=None) -> dict | None
#   {id, user_id, title, target_date, days_left, daily_problems, minutes_per_day,
#    focus_patterns: [slug], phases: [{name, start, end, rules}], current_phase | None,
#    hards_from | None (None = no Hards), hards_allowed, notes, active, created_at, updated_at}
set_plan(user_id, today=None, **fields) -> dict     # create or update; fields: title,
#   target_date, daily_problems 1..20, minutes_per_day 10..600, focus_patterns, phases,
#   hards_from, notes. ValueError on a bad value / unknown pattern / unknown field.
pattern_choices() -> [{slug, name, tier}]           # the focus checkboxes
#   db.init runs plans.migrate_legacy: a user with attempts and no plan row gets PLAN.md
#   (target 2026-12-15, 2/day, 90 min, Tier 1 + Tier 2, phases A/B/C, Hards from 2026-11-01).
#   A brand-new user has none: the web sends him to /onboarding. The backlog's per-day count
#   is the plan's daily_problems (config per_day without a plan); backlog_start stays config.

# the coach's read tools (coach/picks.py), all scoped to user_id
get_profile(user_id, today=None) -> {today, plan, ledger: [{name, kind, pattern, level, trend,
    evidence, updated_on}], patterns: {slug: {name, tier, in_focus, level, clean, attempts,
    owned, last_attempt_day, retired_on, refires}}, recent_attempts: [20 newest: {day,
    number, title, pattern, outcome, target_min, actual_min, bugs, hints, optimal,
    derived_cold, notes}], backlog, streak, clean_rate}
get_due(user_id, today=None) -> {today, retry_due: [{problem_number, title, pattern,
    attempts, age, wait, last_on, reason, blocked, block_reason, overdue_days}],
    retry_waiting: [... due_in_days], refire_due: [{pattern, age, refires_done, last_touch,
    new_variants}], blocklist: [n], today_done, today_target, open_handout_id}
find_problems(user_id, pattern=None, tier=None, untouched=None, difficulty=None, limit=20,
              today=None) -> [{number, title, difficulty, pattern, tier, source, bank_order,
    attempts, last_attempt_day, clean_count, in_retry, blocked, block_reason}]
#   bank order then E -> M -> H, blocked rows last. blocked = hand_out would refuse it now.

# the handoff: the coach picks
hand_out(user_id, number, target_min=None, mode="coached", why="", today=None) -> Handoff
#   raises PickRefused (a ValueError) and writes nothing when: another handout from today is
#   open; the problem was attempted or handed out (skipped too) in the last 7 days; it has 3
#   clean solves (retired); it is a Hard and the plan's hards_from is None or later (no plan:
#   config hards_from); its pattern is burned this session; unknown number; mode not
#   timed|coached; target_min not 1..180. On success: handout stamped now by the server,
#   clock started, `why` -> handouts.pick_reason, `mode` -> handouts.mode, slot inferred
#   (retry | refire | growth | warmup | drill) for reveal and the log, emits `handout`.
#   target_min None = target_for (80% of the first attempt's target on a repeat, else E10 M15
#   H35).
skip_open(user_id) -> {"ok", "handout_id"}           # skip the open handout (still blocks 7 days)
request_next(user_id, source="web") -> int           # queue ONE `next_requested` for the coach

# FALLBACK picker (coach/picker.py): the page's "Use the default pick", bridge next_problem
next_problem(user_id: int, today: date | None = None, force_new: bool = False) -> Handoff | None
#   an open handout from today is returned again (same id and start time);
#   force_new=True marks it skipped and picks again. None = nothing passes the filters.
#   Honours the plan's hards_from like hand_out.
skip_problem(handout_id: int) -> None                       # status -> skipped; still blocks 7 days
reset_timer(user_id: int, handout_id: int) -> dict          # {"ok","handout_id","started_at"}
#   alias of clock(..., "reset", reason="reset"); also emits `handout`.

# the coach's clock (coach/clock.py: elapsed_s, wall_s, state, record, note)
clock(user_id, handout_id, action, reason="", target_min=None, label=None) -> dict
#   action start | pause | resume | reset | target | phase. The coach owns it; nothing is
#   blocked. Every call writes a clock_events row (action, at, reason, value) and emits
#   `clock`. pause/reset without a reason add "warning": "say why". reset: accum 0, anchor
#   now, handed_at now, first_started_at unchanged (wall keeps counting). target: 1..180.
#   phase: leak-guarded label ("" clears). Checkpoints re-arm on reset/target.
#   -> {state running|paused, elapsed_s, wall_s, target_min, phase, anchor_iso, accum_s,
#       pause_reason, pauses, resets, handout_id[, warning]}
clock_state(user_id, handout_id) -> dict | None   # same shape, read only
#   wait_event also pushes `clock_checkpoint` {pct 50|100|150, elapsed_min, target_min,
#   has_approach} once per threshold (only the highest if several are crossed at once;
#   never while paused). Handout target at creation: 80% of the first attempt's target if
#   the problem was attempted before (15 -> 12), else the difficulty default.

log_attempt(user_id: int, handout_id: int | None = None, number: int | None = None,
            actual_min: int | None = None, bugs: int = 0, optimal: bool = True, derived_cold: bool = True,
            hints: int = 0, notes: str = "", abandoned: bool = False,
            target_min: int | None = None, day: date | None = None) -> dict
#   actual_min None (default) = the coach's clock in minutes (min 1); stores wall_min and
#   clock_note ("2 pauses (float precision 4 min; ...), 1 reset") on the attempt.
#   -> {"attempt_id", "outcome": clean|solved|abandoned, "retry": added|bumped|cleared|none,
#       "problem_number", "actual_min", "wall_min", "clock_note"}
#   outcome comes from rules.outcome (0 bugs, <=1.5x target, optimal, derived cold; hints > 0
#   counts as not derived cold). Retry queue: added when over 1.5x, 2+ bugs, a hint, suboptimal,
#   not cold, or abandoned; an already-queued item gets attempts+1; clean clears it.
#   Third clean solve of a pattern writes pattern_state.retired_on; finishing a "refire" handout
#   appends to its refires.

reveal(handout_id: int) -> dict                             # raises RevealBlocked until logged
#   -> {"handout_id","number","title","slot","mode","picked_by": coach|server,"why",
#       "pattern","pattern_name","tier", "clock"}
#   a re-fire handout adds "trap_quiz": {"pattern", "cues": [{"cue", "trap"}]}, parsed by
#   rules.parse_traps from pattern_state.note_md: top-level bullets under a heading starting
#   "Traps" / "Where it bites" / "Pitfalls" / "Gotchas"; `cue -> rule` splits; [] if none.
burn_pattern(user_id: int, slug: str, reason: str = "", today: date | None = None) -> int
#   RULE ZERO leak: pattern is out of play for the current session (the picker honours it)

# re-fires and the trap quiz
record_refire(user_id: int, pattern_slug: str, today=None) -> dict   # {"pattern", "refires"}
#   appends today to pattern_state.refires, once per day (log_attempt on a re-fire handout
#   already does). ValueError if the pattern has no state row.
log_trap_recall(user_id, pattern_slug, trap, cue, recalled: bool, hit_in_code: bool,
                today=None) -> dict
#   -> {"id","date","pattern","trap","cue","recalled","hit_in_code"}  (data/trap_recall.csv
#   columns; booleans stored "yes"/"no")

# ledger and session log (session end)
set_skill(user_id, name, level: int, evidence="", trend="", today=None) -> dict
#   upsert by name (exact, case-insensitive, then pattern slug) + a skill_events row dated today.
#   New skill: kind "pattern" if name is a pattern slug/name, else "meta". ValueError unless 0-5.
#   -> {"skill_id","name","was","level","trend","evidence","created"}
write_session_log(user_id, markdown, today=None) -> dict     # {"session_id", "chars"}
#   sessions.log_md of today's latest session (ended or not); creates the row if none.
session_summary(user_id, today=None) -> dict
#   {"today","session_id","problems":[{handout_id,number,title,handed_at,target_min,status,
#     mode,attempt|None,reveal|None}],"attempts_without_handout","skill_changes":[{name,level,
#     evidence}],"retry":{"added","cleared","bumped"},"trap_recall":[...]}
#   reveal (slot, pattern, pattern_name, tier, picked_by, why) only for LOGGED handouts.
#   Progress talk.

set_statement(user_id: int, handout_id: int, text: str) -> dict   # {"ok", "handout_id", "number"}
#   coach-written statement, stored in problems.paraphrase (overwrites), returned in Handoff.paraphrase.
#   LEAK GUARD: raises ValueError("leak: word, word") and stores nothing if text names a pattern
#   (patterns table names/slugs + synonyms such as sliding window, heap, dp, greedy) or easy/medium/
#   hard. Case-, hyphen- and plural-insensitive. A term the problem's own title contains is allowed.
get_statement(handout_id: int) -> str | None

# visuals: coach-drawn illustrations under the statement (coach/viz.py renders)
show_visual(user_id, handout_id, kind: str, spec: dict) -> dict   # {"id", "html"}
#   kind: array | grid | list | tree | steps | io | html | animate (spec schema: viz.py / SKILL.md).
#   animate: {base: array|list|tree|grid|steps, frames: [{spec, caption}] (2-40), loop, ms
#   (300-6000, default 1200)}; every frame spec is validated and leak-checked like a still.
#   LEAK GUARD on every text in the spec (caption, labels, pointer names, columns, cells, io
#   strings, html text content) via leak_words + the problem title: ValueError("leak: ...")
#   and nothing stored or posted. Bad kind/spec: ValueError. On success it stores a `visuals`
#   row and posts it as a coach message (content = caption or kind label, visual_id = the
#   visual) with handout_id: the thread renders the visual in a coach bubble. Returns
#   {"id", "html", "message_id"}.
visuals(user_id, handout_id, since_id=0) -> list   # [{"id","kind","caption","html","created_at"}]
count_visuals(user_id, handout_id) -> int
clear_visuals(user_id, handout_id) -> int          # rows marked cleared (cleared_at); their
#   thread bubbles render as a muted "Illustration cleared." line. visuals()/count_visuals()
#   skip cleared rows.
open_handout_id(user_id) -> int | None             # latest open handout (the bridge uses it)

# progress (see RULE ZERO)
status(user_id: int, today: date | None = None) -> dict
#   {"today", "backlog": {backlog,target_today,done_today,remaining,days,owed_before,done_before,start},
#    "streak": {current,longest,last14},
#    "tiers": {tier1:[{pattern,tier,clean,needed,tried,status}], tier2:[...], owed, clean_rate,
#              tier2_open, attempts_needed, ready_1_per_day, ready_at_55},
#    "retry": [{problem_number,title,attempts,age,wait,due,last_on,reason}],
#    "refire": [{pattern,age,due,refires_done,last_touch}],
#    "blocklist": [numbers], "weakest": [5 x skill], "strongest": [skill, level 4+],
#    "accountability": {last_practice_day, days_missed, missed_days_list: [dates]}}
#   missed_days_list = calendar days strictly between the last practice day and today
#   (rules.missed_days).
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
last_message_id(user_id) -> int                    # 0 if none

# live page signal (ui_events; the web process streams it as SSE on /stream)
emit(user_id: int | None, kind: str, ref_id: int | None = None, s=None) -> int
#   append a ui_events row (user_id None = every user); prunes rows older than 1 h. Pass the
#   caller's session `s` to commit with the data. kind and who emits it:
#     message   post_message (and show_visual's chat line)        ref = message id
#     statement set_statement                                     ref = handout id
#     visual    show_visual (ref = visual id), clear_visuals (ref = handout id)
#     editor    unlock_editor / lock_editor                       ref = handout id
#     handout   next_problem when a new handout is made or force_new skips one, skip_problem,
#               log_attempt, reset_timer                          ref = handout id
#     clock     clock (every action)                              ref = handout id
#     presence  coach_seen(user_id), at most once per 5 s          that user
ui_events_since(user_id, since_id=0, limit=200) -> list   # [{"id","kind","ref_id"}] (his + broadcast)
ui_last_id() -> int

# coach presence (per user: users.coach_last_seen, UTC iso)
coach_seen(user_id, now=None)          # heartbeat; every authenticated /mcp or /tools request
                                       # and every wait_event poll; write throttled to 5 s
coach_online(user_id, now=None) -> {"online", "last_seen", "reason"}
#   online: heartbeat <= 90 s old, or a coach message to THIS user in the last 10 min.

# one attempt per handout
log_attempt(...) for a handout that already has an attempt -> the existing attempt's
#   {attempt_id, outcome, problem_number, actual_min, wall_min, clock_note, retry: "none",
#   already_logged: True}; nothing written. Unique index uq_attempts_handout (partial:
#   handout_id not null); db.init adds it to old databases unless duplicates already exist.
#   A handout of another user -> ValueError("no handout N").
```

Exception: `service.RevealBlocked`.

## Other modules the builders may use

* `coach.db`: `init(path=None)`, `session_scope()` (context manager), `new_session()`,
  `add_missing_columns(engine)`: run by `init`; adds model columns an existing sqlite file
  lacks (additive, nullable only). First use: `skills.updated_on`.
* `coach.models`: all tables (users, plans, patterns, problems, attempts, handouts, sessions, skills,
  skill_events, retry_items, pattern_state, burned, oa_sims, trap_recall, config, events,
  coach_messages, visuals, ui_events, coach_tokens).
* `coach.lint.lint(src) -> [(line, kind, msg)]`, kinds USELESS, NONE-ASSIGN, COST, ALIAS, SYNTAX.
  Not wired into `log_attempt`; the caller runs it on the pasted code.
* `coach.rules`: the pure functions (`backlog`, `streak`, `tier_progress`, `retry_due`,
  `refire_due`, `blocklist`, `outcome`, `report_card`, ...), taking rows + `today`.
* `coach.picker.pick_next(session, user_id, today)` is the lower level; prefer `next_problem`.

## Coach tools (`coach/tools.py`, served by `/mcp`, `/tools` and `bridge/server.py`)

One registry: each tool is `fn(ctx, **args) -> dict`, `ctx.user_id` from the Bearer coach
token (`coach/tokens.py`, `coach_tokens` table, sha256 only) or COACH_USER for the local
bridge. No tool takes a user id. Transports: `coach/web/remote.py` (`/mcp` Streamable HTTP,
`GET /tools`, `POST /tools/<name>`: 401 auth, 404 unknown, 422 bad args, tool {error} = 200,
`wait_for_event` capped at 55 s per REST call) and `bridge/server.py` (stdio: direct DB, or
`--remote URL --token T` forwarding to `/tools`). Tokens: `/connect` page,
`python -m coach.cli coach-token NAME [--label L] [--revoke ID] [--list]`.

Choosing: `get_profile()`, `get_due()`, `find_problems(pattern, tier, untouched, difficulty,
limit)` -> {candidates}, `hand_out(number, target_min=None, mode="coached", why="")` ->
handoff | {error} (posts the handoff text and "Statement coming." like next_problem),
`skip_problem()` -> {ok, handout_id} | {error}. `next_problem` is the FALLBACK pick.
`say` is pre-checked with `service.check_coach_text`: a RULE ZERO refusal returns
{error, posted: false} before any clock action.

`coach_status`, `next_problem`, `set_statement`, `wait_for_event`, `say`, `log_attempt`,
`reveal`, `burn_pattern`, `lint`, `report`, and (new) `log_trap_recall`, `record_refire`,
`set_skill`, `write_session_log`, `session_summary`, `show_visual(kind, spec)` ->
{ok, id} | {error}, `clear_visuals()` -> {ok, cleared}, `reset_timer()` -> {ok, handout_id,
started_at} | {error}, `clock(action, reason, target_min, label)` -> clock state | {error},
`say(text, clock_action=None, clock_reason="", target_min=None, label=None)` (clock first;
on a clock error nothing is posted) (all act on the open handout). Chat side effects:

* `next_problem` posts the handoff text (never the mode), then "Statement coming." when
  `paraphrase` is null.
* `set_statement` stores the statement for the Problem tab only; it posts nothing to the chat. The coach explains the question in chat itself (skill: "Explaining the question").

## Web: Next problem, plan, onboarding

* `POST /solve` (Next problem) never picks. Coach online: `request_next` queues one
  `next_requested` event, then `/next`. Offline: straight to `/next`. Open handout: `/solve`.
* `GET /next` is the neutral waiting page: "Your coach is choosing your next problem…". It
  follows the coach's `handout` ui_event to `/solve` (live stream, same as the interstitial).
  Offline (or the coach drops while waiting): the connect steps, "Ask my coach" (POST /solve
  again) and "Use the default pick" (`POST /solve/default` = `next_problem`, pushes the
  `handout` event {source: "web"} to the coach queue as before). `?none=1`: nothing eligible.
* `/onboarding` (GET/POST): a user without a plan is sent here from `/`, `/dashboard`,
  `/plan` and `POST /solve`. Fields: target date, problems a day, minutes a day, focus
  checkboxes, when to allow Hards (from the start | from a date | not yet).
* `/plan` (GET/POST, `?saved=1`): view / edit the plan (also title, phases one per line
  `name | start | end | rules`, notes). Linked from the dashboard only; like the dashboard it
  redirects to `/solve` while a problem is open.
* `/reveal/{id}` shows the coach's `why` ("Why your coach picked it").

## Web: visuals

`GET /visuals?since=N` (login) -> JSON `[{id, html, caption, kind}]` for the open handout,
ids > N. Headers: `X-Visual-Count` (total for the handout; fewer than shown = cleared, the
page rebuilds), `X-Open-Handout`. Kept as an API; the page no longer uses it. Page side:
visuals are coach messages: `list_messages` rows carry `visual` {id, kind, caption, html,
cleared} (or null), `_messages.html` renders the html in the bubble (scrolls sideways
inside it), and a `visual` event makes `solve.js` swap cleared bubbles. `static/viz.css`
(fetches on each `visual` event) + `static/viz.css`.

## Web: live stream

`GET /stream` (login) is `text/event-stream`. Cursor: `?since=<ui_events id>`, else the
`Last-Event-ID` header, else "from now". First event `hello` {open_handout_id, editor_open,
started_at, online, last_message_id} (id = the cursor), then one event per ui_events row
(`id: <row id>`, `event: <kind>`, `data: {"ref_id": ..}`), `presence` {"online"} only when
coach_online(user) flips (re-checked every 2 s too, so going offline shows), `: ping` every 15 s.
The DB is read every 250 ms. `max_events` / `max_s` stop the stream early (tests only).

`GET /handout/state` (login) -> {open_handout_id, editor_open, started_at, target_min,
elapsed_s} (all null/false with nothing open).

Page side: `static/live.js` (loaded by `_coach_banner.html`) holds the one EventSource,
reconnects with backoff, and exposes `coachLive.on(kind, fn)`, `coachLive.onState(fn)`.
`resync` fires after every `hello` and, only while the stream is failed, every 20 s. Handlers:
message -> `/messages?since=`; statement -> `/statement`; visual -> `/visuals?since=`;
editor / handout -> `/handout/state` (open/lock the editor, restart the clock when
started_at changes, navigate when the open handout changes: reveal if closed, else /solve);
presence -> banner + Live dot. The `/` interstitial goes to `/solve` when a handout opens.

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

* Fallback picker only: slot order per session is warm-up, drill, growth, retry, cycling by
  the number of handouts in the open session; a slot with no candidate falls through.
* A session is opened lazily (`next_problem`, `burn_pattern`, `start_session`) for `today`;
  burned patterns are per session. Call `end_session` to close one.
* Imported history has `attempts.source = "import"`; new rows are `"app"`. Re-running the
  migration refuses once `"app"` attempts exist unless `--force`.
* The migrated owner user is `chandan` with no password: run `create-user chandan` to set it.

## Tests panel (coach/tests_api.py, coach/runner.py; re-exported by service)

```python
runner.run(code, cases, mode, entry, timeout_s=3) -> [{case_index, ok, got, expected, error,
                                                       stdout, ms}]
#   subprocess: base python -I in a fresh temp dir, env PATH/SYSTEMROOT only, stdin closed,
#   one wall timeout per run (unfinished cases: error "timeout"), stdout/got capped at 64 KB.
#   ok None = case has no expected. Floats 1e-5; bool != int. Never imports his code.
#   function: case {args: {name: value}, expected}, entry {class: "Solution", method}
#   design:   case {ops, args, expected}, entry {class}; first op constructs (null output)
#   options:  types {arg|"return": ListNode|TreeNode}, unordered true|"deep", inplace "arg", label
set_tests(user_id, handout_id, cases, mode="function", entry=None) -> {ok, handout_id, count}
#   replaces the coach cases (his stay) + test_specs row; LEAK GUARD on label/note; emits `tests`
add_user_test(user_id, handout_id, case) -> {id, source: "user", ...case}   # emits `tests`
delete_user_test(user_id, handout_id, case_id) -> {ok}                       # his only
tests(user_id, handout_id) -> {handout_id, mode, entry, cases: [{id, source, ...}], last_run}
#   last_run = his latest run {id, who, at, results, passed, failed, ran, total} | None
run_tests(user_id, handout_id, code, who="user", cases=None) -> {run_id, results, passed,
                                                                failed, ran, total}
#   stores test_runs. who=user: emits `tests`, push_event "run" {handout_id, run_id, passed,
#   failed, ran, total, results_summary}, posts system line "Ran tests: 2/3 passed.".
#   who=coach: stored only (no event, no chat, not in last_run). cases= coach only, not stored.
last_run(user_id, handout_id, who="user") -> run | None
```

Tables `test_specs` (handout_id pk, mode, entry), `test_cases` (source coach|user, case json),
`test_runs` (who user|coach, code, results json). ui_event kind `tests` (ref = handout id).
Web (login): `GET /tests`, `POST /tests/run` (code), `POST /tests/add` (case = json),
`POST /tests/delete` (id); errors 400 {error}. Page: `_tests.html`, `static/tests.js`,
`static/tests.css`. Bridge: `set_tests(cases, mode, entry)`, `run_tests(cases=None)` (his
current draft via read_code, else the last submission; {error: "no code yet"}).
