# Claude Code bridge

The coach is Claude Code running on this machine. The website is where he works. They meet
in one sqlite file (`COACH_DB`):

    browser --push_event--> events table --wait_for_event--> Claude Code (the /coach skill)
    browser <--messages---- messages table <------say-------- Claude Code

`wait_for_event` is a blocking MCP tool. Claude calls it at the end of every turn and it
returns when the website pushes something (approach, submission, stuck, skip, chat,
timer_over), or `{kind: "timeout"}` after 25 min. No channels, no notifications.

Files:

    bridge/server.py              stdio MCP server (python -m bridge.server)
    bridge/install.py             writes .mcp.json with this machine's absolute paths
    bridge/skills/coach/SKILL.md  the /coach skill: the coaching rules, drives the session
    .claude-plugin/plugin.json    plugin manifest (skill + MCP server)
    .claude-plugin/marketplace.json  local marketplace so the plugin installs in one step

## Install — pick ONE of these

**A. Plugin (skill + server together).** From any shell:

    claude plugin marketplace add D:/Git/virtual457-projects/leetcode-coach
    claude plugin install leetcode-coach@leetcode-coach-local

The skill is then `/leetcode-coach:coach` (plugin skills are namespaced; typing `/coach`
should find it in the picker).
The plugin's server uses `${CLAUDE_PLUGIN_ROOT}/.venv` and `${CLAUDE_PLUGIN_ROOT}/coach.db`
— the repo itself, since a local marketplace is loaded in place.

**B. Project server only.** In the repo:

    .venv/Scripts/python -m bridge.install            # writes .mcp.json
    .venv/Scripts/python -m bridge.install --user NAME --db D:/path/coach.db

Claude Code asks once to approve the project server when you start it in the repo.
`bridge.install` also prints the user-scope one-liner, which registers the same server for
every project without a file:

    claude mcp add -s user -e COACH_DB="<repo>\coach.db" -e PYTHONPATH="<repo>" \
      --transport stdio coach -- "<repo>\.venv\Scripts\python.exe" -m bridge.server

Don't run A and B at once: you get two `coach` servers and every tool twice.

Env: `COACH_DB` (sqlite path), `COACH_USER` (username; unset = the first user). The
per-server `"timeout": 1800000` (30 min) keeps the 25-min wait from being cut. The server
also sends a progress ping every 30 s, so Claude Code's stdio idle timeout never fires.

## How a day runs

1. Start the website, open it in the browser, log in.
2. Open the Claude desktop app (or `claude`) in any folder. Type `/coach`.
3. Coach posts the backlog line and "Ready?" in the browser chat panel, then waits.
4. Switch to the browser. Everything happens there: say yes, get the problem, type the
   approach, submit, chat. Each action wakes Claude; it replies in the chat panel and
   waits again. After ~2 min Claude Code may move the wait to the background — fine, the
   result still lands.
5. Say you're done for the day. Coach posts headline numbers and stops.

## Test the wait tool by hand

Automated: `.venv/Scripts/python -m pytest tests/test_bridge.py -q`

Manual, against a real session:

1. In Claude Code ask: "call the coach wait_for_event tool with timeout_s 600".
2. In another shell push an event into the same DB:

       COACH_DB=<same db> .venv/Scripts/python -c "from coach import service as s; s.push_event(s.default_user_id(), 'chat', {'text': 'hello'})"

3. The tool returns `{"kind": "chat", "payload": {"text": "hello"}}` within a second.
   With nothing pushed it returns `{"kind": "timeout"}` when the timeout passes.

Inspect tools without Claude: `npx @modelcontextprotocol/inspector .venv/Scripts/python -m bridge.server`.
