# Deploy

Three ways to run the web app. The MCP bridge (Claude Code side) always runs on your machine.

## Environment variables

| Var | Used by | Meaning |
|---|---|---|
| `COACH_DB` | web, CLI, bridge | SQLite file path. Default `./coach.db`. Docker image sets `/data/coach.db`. |
| `COACH_DB_URL` | web, CLI | Any SQLAlchemy URL (for example `postgresql+psycopg://user:pw@host/db`). Wins over `COACH_DB`. The bridge ignores it while `COACH_DB` is set (see Postgres below). |
| `COACH_SECRET` | web | Signs the session cookie. If unset, one is generated into `.env` (`COACH_ENV_FILE` moves it). Set it explicitly on any hosted box. |
| `COACH_ENV_FILE` | web | Where the generated secret is stored. Docker image: `/data/.env`. |
| `COACH_USER` | bridge | Username the bridge acts as. Unset = the first user. |
| `COACH_PASSWORD` | CLI | Non-interactive `create-user`. |
| `PORT` | Docker | Listen port, default 8731. |

## 1. Local (Windows)

    python -m venv .venv
    .venv/Scripts/python -m pip install -r requirements.txt
    .venv/Scripts/python -m coach.cli create-user chandan
    ./start.ps1                      # http://127.0.0.1:8731

Binds to 127.0.0.1 only. Nothing else to configure.

## 2. Docker

    docker compose up --build        # http://127.0.0.1:8731
    docker compose exec coach python -m coach.cli create-user chandan

Data lives in the `coach-data` volume (`/data/coach.db`). The container runs as a non-root
user. Set `COACH_SECRET` in your shell or in a `.env` next to the compose file for a stable key.

Not verified: `docker build` was not run when this was written (the Docker daemon was not
running). Run the build once before relying on it.

## 3. Fly.io

One small machine plus a volume, roughly $3-5 a month.

    cp fly.toml.example fly.toml            # edit `app`
    fly apps create your-leetcode-coach
    fly volumes create coach_data --size 1 --region iad
    fly secrets set COACH_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
    fly deploy
    fly ssh console -C "python -m coach.cli create-user chandan"

Load existing data: copy `coach.db` to `/data/coach.db` with `fly ssh sftp shell` while the app
is stopped, or run `migrate` on the machine against a copy of the old repo.

Rules for SQLite on Fly: exactly one machine, one volume. Do not scale to two machines; each
would have its own file. `auto_stop_machines` is fine for one user. Not verified: the Fly steps
and `fly.toml.example` were written from docs, not run.

## Backups

    ./scripts/backup.ps1                    # Windows, copies coach.db to .\backups\
    ./scripts/backup.sh [db] [dir]          # Linux/macOS/Docker host, uses sqlite3 .backup if present

Both write `coach-YYYYMMDD-HHMMSS.db` and keep the newest 30. Run from Task Scheduler or cron
daily. On Fly: `fly ssh sftp get /data/coach.db`, or `fly volumes snapshots list`. The previous
coaching system was lost because it was never committed; do not repeat that with the database.
Copy backups off the machine.

## HTTPS

Never expose port 8731 over plain HTTP outside localhost. Fly terminates TLS
(`force_https = true`). Behind your own proxy, terminate TLS there; the container runs uvicorn
with `--proxy-headers`. The login is a password plus a signed cookie, and HTTPS is what protects
both.

## Postgres (optional, untested)

Set `COACH_DB_URL=postgresql+psycopg://...` and install `psycopg[binary]`. Tables are created on
start. Things to know:

- The models use only portable SQLAlchemy types (String, Integer, Float, Boolean, Date, DateTime,
  Text, JSON). No sqlite-only constructs were found in `coach/`, `migrate/` or `bridge/`. The
  `PRAGMA foreign_keys` call in `coach/db.py` now runs for sqlite only.
- There is no Alembic. `create_all` creates missing tables, and `db.add_missing_columns`
  (added by another change) adds missing nullable columns. Anything else (renames, type changes,
  constraints) is not handled. Add Alembic before such a change on a hosted database.
- `wait_event` in the bridge polls the DB every 0.25 s. Fine on sqlite, noisy on a remote DB.
- The 74 pytest tests run on sqlite only. Postgres was not tested at all.
- `bridge/server.py` passes `COACH_DB` explicitly to `db.init()`, and an explicit path beats
  `COACH_DB_URL`. To point the bridge at Postgres, set `COACH_DB_URL` and leave `COACH_DB` unset.
- `postgres://` URLs are rewritten to `postgresql://`.

## Architecture note: hosted server, local bridge (future, not built)

Claude Code is the coach and it runs on your machine. A hosted site cannot reach it. Today the
two meet in one SQLite file, so the bridge must see the same DB as the web app. For a hosted
server the plan is:

    browser --> hosted web app --> events/messages tables (hosted DB)
                                         ^
    Claude Code <--stdio--> local bridge --+  (outbound HTTPS/WebSocket, per-user token)

The bridge stays local and dials out to the hosted server, so no inbound port on your laptop.
It would call an authenticated API (`wait_for_event`, `say`, `next_problem`, `log_attempt`, ...)
instead of opening the DB file. That API and the token auth do not exist yet. Until they do, a
hosted web app is only useful if the bridge reaches the same Postgres through `COACH_DB_URL`,
which exposes the database to the internet. Prefer local use for now.
