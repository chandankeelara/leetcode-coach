# Cutover: retire the markdown repo

Goal: state lives in the coach app. The old repo (`D:\Git\virtual457-projects\leetcode`) stops
being the source of truth. The old repo is only read until step 6.

The migration refuses to re-run once the app has its own attempts unless `--force`, so migrate
before the app has any.

1. Freeze the old repo. Finish the day's work there and commit it. Anything written in markdown
   after the last migrate is not in the app.
2. Back up the app DB if it exists: `./scripts/backup.ps1`.
3. Migrate:
   `.venv/Scripts/python -m coach.cli migrate D:/Git/virtual457-projects/leetcode`
   Safe to re-run until step 6 (idempotent).
4. Verify: `.venv/Scripts/python -m coach.cli verify`
   Expect all PASS except the known 7-day blocklist difference (status.py blocks 102/200/239/503
   because of a stray date on line 67 of `sessions/2026-09-22-session-08.md`, and misses 424
   until today's log is written; the app is right on both). Any other failure: stop and fix it.
5. Run one real day in the app:
   - `create-user` if the user has no password yet.
   - Register the bridge (plugin or `bridge.install`, see `bridge/README.md`).
   - `./start.ps1`, open Claude Code, `/coach`, work in the browser.
   - Write today's session log in the app, then run `coach status` and check the backlog,
     streak and retry list against what you expect.
6. Replace the old repo's `CLAUDE.md` with the draft below. Leave the other markdown files in
   place as the fallback. Commit in the old repo.
7. From now on nobody edits `SKILL_LEDGER.md`, `RETRY_QUEUE.md`, `data/attempts.csv` or
   `sessions/`. `python tools/status.py` may still be run as a cross-check.
8. After a week with no surprises, archive the old repo (tag it or move it under `archive/`).
   RULE ZERO is also encoded in the app (`Handoff` DTO, page rules, README).
9. Schedule `scripts/backup.ps1` and copy backups off the machine.

Rollback: restore the old `CLAUDE.md` with `git checkout HEAD~1 -- CLAUDE.md` in the old repo.
The markdown files are untouched.

## Draft replacement CLAUDE.md (do not apply until step 6)

````markdown
# CLAUDE.md

This repo is the old, frozen markdown record of a LeetCode interview-prep coaching system.
State now lives in the coach app (`leetcode-coach`). Use the coach MCP tools; do not edit the
markdown files here (`PLAN.md`, `SKILL_LEDGER.md`, `RETRY_QUEUE.md`, `data/`, `sessions/`,
`patterns/`).

## To coach
Use the coach MCP tools from the `leetcode-coach` plugin, through the `/leetcode-coach:coach`
skill: `coach_status`, `next_problem`, `wait_for_event`, `say`, `log_attempt`, `reveal`,
`burn_pattern`, `lint`, `report`. The learner works in the coach website; you answer in its chat
panel with `say`.

## RULE ZERO, still binding
The thing being measured is whether HE recognises the pattern. Never leak it.
- Progress talk and a problem handoff never share a message, and progress is never the message
  right before a handoff.
- A handoff is only: number, title, statement, examples, constraints, start time, target, and
  "give an approach".
- If a pattern leaks, call `burn_pattern`. It is out for the session.
- Talk about progress only after a problem is logged, looking backwards.

## Coaching rules
Socratic only: ask a question or hand over a failing input, never point at a bug. Concrete
first: a counter-example before an abstract explanation. Approach in plain words before code.
Caveman English: short, no fluff, no praise padding. Never repeat a problem within 7 days.
Language-semantics bugs get taught properly.

## Do not
Edit the markdown files, treat `tools/status.py` as the source of truth (cross-check only), or
open `reference/` before a cold, timed reproduction is logged.
````
