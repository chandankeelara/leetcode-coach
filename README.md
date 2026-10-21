# leetcode-coach

Personal LeetCode coaching app: state store, CLI, and a local web UI.

## Setup

    python -m venv .venv
    .venv/Scripts/python -m pip install -r requirements.txt

    .venv/Scripts/python -m coach.cli create-user chandan     # prompts for a password
    .venv/Scripts/python -m coach.cli migrate <old-repo-path>  # idempotent
    .venv/Scripts/python -m coach.cli verify                   # must match the old status.py
    ./start.ps1                                                # http://127.0.0.1:8731

Or in Docker: `docker compose up --build`. See `docs/DEPLOY.md` (local, Docker, Fly.io, backups,
env vars, HTTPS), `docs/CUTOVER.md` (retiring the markdown repo) and `docs/PRODUCT.md` (roadmap).

Env: `COACH_SECRET` signs the session cookie (generated into `.env` on first run if unset).
`COACH_DB` picks the SQLite file (default `./coach.db`). `COACH_DB_URL` takes any SQLAlchemy URL
and wins over `COACH_DB`. `COACH_USER` picks the bridge's user.

Backup: `./scripts/backup.ps1` (keeps the newest 30).

Tests: `.venv/Scripts/python -m pytest`

## Coach-written statements

The statement on `/solve` is not scraped from LeetCode. When a handout's `paraphrase` is empty the
coach (Claude, through the bridge's `set_statement` tool) writes the statement, examples and
constraints in its own words; it is stored on the problem and reused next time. `set_statement`
refuses text that names a technique (any pattern name plus synonyms like "sliding window" or
"heap") or a difficulty, except words the problem's own title contains ("Reverse Linked List").
`/solve` shows a Statement card above the approach box, rendered as light markdown with HTML
escaped first; until it exists the card says the coach is writing it and `solve.js` polls
`GET /statement` (JSON `{handout_id, html}`).

## RULE ZERO: page rules

The thing being measured is whether the learner recognises the pattern himself. So:

- `/` (when nothing is open) is a neutral interstitial: one button, no numbers, no progress.
  The button asks the coach, who picks by the learner's plan (`/next` waits neutrally; offline
  it offers the default pick). A new user sets up the plan at `/onboarding` first.
- `/solve` and `/log` show only the problem: number, title, link, paraphrase, timer, target,
  instruction, chat. No pattern, slot, tier, difficulty, backlog or streak.
- Progress lives only on `/dashboard`, which redirects to `/solve` while a problem is open.
- `/reveal/{id}` shows pattern, slot and outcome, and only after the attempt is logged.
- "Next" on reveal and dashboard goes to `/`, never straight to `/solve`. Progress and a handoff
  are never on the same page or on adjacent ones.
- The start time is the server's. The user never types it.
- `tests/test_web.py` checks the solve page and the interstitial for pattern names and the
  words slot, tier, backlog, streak, easy, medium, hard.
