# leetcode-coach

Personal LeetCode coaching app: state store, CLI, and a local web UI.

## Setup

    python -m venv .venv
    .venv/Scripts/python -m pip install fastapi uvicorn jinja2 python-multipart itsdangerous argon2-cffi httpx pytest sqlalchemy websockets

    .venv/Scripts/python -m coach.cli create-user chandan     # prompts for a password
    .venv/Scripts/python -m coach.cli migrate <old-repo-path>  # idempotent
    .venv/Scripts/python -m coach.cli verify                   # must match the old status.py
    ./start.ps1                                                # http://127.0.0.1:8000

`COACH_SECRET` signs the session cookie. If it is not set, it is generated into `.env` on
first run. `COACH_DB` picks the SQLite file (default `./coach.db`).

Tests: `.venv/Scripts/python -m pytest`

## RULE ZERO: page rules

The thing being measured is whether the learner recognises the pattern himself. So:

- `/` (when nothing is open) is a neutral interstitial: one button, no numbers, no progress.
- `/solve` and `/log` show only the problem: number, title, link, paraphrase, timer, target,
  instruction, chat. No pattern, slot, tier, difficulty, backlog or streak.
- Progress lives only on `/dashboard`, which redirects to `/solve` while a problem is open.
- `/reveal/{id}` shows pattern, slot and outcome, and only after the attempt is logged.
- "Next" on reveal and dashboard goes to `/`, never straight to `/solve`. Progress and a handoff
  are never on the same page or on adjacent ones.
- The start time is the server's. The user never types it.
- `tests/test_web.py` checks the solve page and the interstitial for pattern names and the
  words slot, tier, backlog, streak, easy, medium, hard.
