# Coach tools and how Claude Code connects

The coach is each student's own Claude Code. The website holds the database and serves the
coach's tools, so a student installs nothing but Claude Code:

    browser --push_event--> events table --wait_for_event--> Claude Code (the /coach skill)
    browser <--messages---- messages table <------say-------- Claude Code

Every tool is defined once, in `coach/tools.py`. Three transports build from it:

    /mcp            remote MCP, Streamable HTTP, on the website   coach/web/remote.py
    GET/POST /tools plain REST, on the website                    coach/web/remote.py
    bridge.server   local stdio MCP: forwarding to /tools, or direct DB for development

`/mcp`, `/tools` and the forwarding bridge expose identical tool names and schemas (tested).
Auth for both website transports: `Authorization: Bearer <coach token>`. A token is a user:
every call runs as the token's user, and no tool takes a user id. Tokens are stored as sha256
only; unknown or revoked -> 401. No login cookie, no CSRF on these paths.

`wait_for_event` is a blocking tool. Claude calls it at the end of every turn; it returns
when the website pushes something (approach, submission, stuck, skip, chat, timer_over, ...)
or `{kind: "timeout"}`. Over MCP it sends a progress notification every 30 s and stops when
the client cancels. Over REST it is a long poll of at most 55 s per request (call again on
`timeout`).

## Tokens

* Students: the site's **Connect coach** page (`/connect`, linked from the dashboard and
  `/help/connect`). "New token" shows the token once, with the commands filled in. Revoke
  there too. Label, created and last used are listed.
* Admin / CLI: `python -m coach.cli coach-token NAME [--label L]` prints a new token once;
  `--list`, `--revoke ID`.

## Connecting

Pick ONE. Two `coach` servers give every tool twice.

**(a) Remote MCP (normal).** In any shell:

    claude mcp add --transport http coach https://<site>/mcp --header "Authorization: Bearer <token>"
    claude mcp list            # coach ... Connected

Add `--scope user` to have it in every project. Then type `/coach` in Claude Code (the skill
comes with the plugin below, or copy `bridge/skills/coach` into `~/.claude/skills/`).

**(b) Forwarding bridge, where MCP over HTTP is blocked.** A local stdio MCP server that
talks plain HTTPS to `/tools`. Needs Python 3.12 and this package (`pip install <repo>`):

    claude mcp add --transport stdio coach -- python -m bridge.server --remote https://<site> --token <token>

(or env `COACH_URL` / `COACH_TOKEN` instead of the flags). It lists tools from
`GET /tools`, forwards calls to `POST /tools/<name>`, and loops 55 s long polls for
`wait_for_event`, with progress pings meanwhile.

**(c) Local dev bridge (direct DB, no website auth).** For working on this repo:

    .venv/Scripts/python -m bridge.install            # writes .mcp.json (stdio, COACH_DB)
    .venv/Scripts/python -m bridge.install --user NAME --db D:/path/coach.db

or by hand:

    claude mcp add -s user -e COACH_DB="<repo>\coach.db" -e PYTHONPATH="<repo>" \
      --transport stdio coach -- "<repo>\.venv\Scripts\python.exe" -m bridge.server

Env: `COACH_DB` (sqlite path), `COACH_USER` (username; unset = the first user).

### Plugin

`.claude-plugin/plugin.json` ships the `/coach` skill and the remote server (a):

    "coach": { "type": "http", "url": "${user_config.coach_url}/mcp",
               "headers": { "Authorization": "Bearer ${user_config.coach_token}" } }

`coach_url` (default `http://127.0.0.1:8731`) and `coach_token` (sensitive, kept in the OS
credential store) are `userConfig` options: Claude Code asks for them when the plugin is
enabled, and `/config` changes them. Install:

    claude plugin marketplace add <path or repo of this project>
    claude plugin install leetcode-coach@leetcode-coach-local

The skill is then `/leetcode-coach:coach`. If your Claude Code is too old for `userConfig`
in MCP headers, skip the plugin's server: disable it and use the (a) one-liner, which does
the same. `"timeout": 1800000` (30 min) keeps the 25-min wait from being cut.

Plugin users before 0.2.0 had a stdio server pointing at the repo's `coach.db`; after this
update reinstall the plugin (`claude plugin update leetcode-coach@leetcode-coach-local`, or
uninstall + install) and enter the site URL and a token.

## Coach presence

Per user. Every authenticated `/mcp` or `/tools` request and every `wait_event` poll writes
`users.coach_last_seen` (UTC, at most every 5 s). The site treats THAT user's coach as online
when it is under 90 s old, or when a coach message to that user was posted in the last
10 min. `/messages` returns `X-Coach-Online: 1|0`, `GET /coach/status` returns
`{online, last_seen}`, `/stream` sends `presence` when it flips. Steps for the learner:
`/help/connect`.

## How a day runs

1. Open the website, log in.
2. Open the Claude desktop app (or `claude`) in any folder. Type `/coach`.
3. Coach posts the backlog line and "Ready?" in the browser chat panel, then waits.
4. Switch to the browser. Everything happens there. Each action wakes Claude; it replies in
   the chat panel and waits again.
5. Say you're done for the day. Coach posts headline numbers and stops.

## Test by hand

Automated: `.venv/Scripts/python -m pytest tests/test_bridge.py tests/test_remote.py -q`

REST, against a running site:

    curl -H "Authorization: Bearer <token>" https://<site>/tools
    curl -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
         -d '{"timeout_s": 30}' https://<site>/tools/wait_for_event

Push an event into the same DB from another shell and the wait returns it within a second:

    COACH_DB=<db> .venv/Scripts/python -c "from coach import service as s; s.push_event(s.default_user_id(), 'chat', {'text': 'hello'})"

Inspect the dev bridge without Claude: `npx @modelcontextprotocol/inspector .venv/Scripts/python -m bridge.server`.

Deploy note: MCP sessions live in the web process's memory (stateful Streamable HTTP), and
the `wait_for_event` event buffer is per process. Run one web worker, or sticky sessions.
