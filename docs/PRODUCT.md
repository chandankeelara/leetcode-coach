# Product roadmap

Plain version. Efforts are rough, for one developer, in focused days.

## Where it is
A personal tool. One user, local web app, SQLite, Claude Code as the coach through a local MCP
bridge, $0 API cost. The data model already carries user ids, so multi-user is a change of
plumbing, not of schema. RULE ZERO (never leak the pattern to the learner) is enforced by the
`Handoff` DTO and the page rules. That is the main thing that sets this apart from a problem
list.

## Who picks the problem
The coach does. Each learner has a plan (target date, problems and minutes a day, focus
areas, phases, when Hards open), set at onboarding and editable from the dashboard. When he
presses Next problem the page asks the coach and waits; the coach reads his plan, skill
ledger, per-pattern record, due retries and re-fires through the server (`get_profile`,
`get_due`, `find_problems`) and hands one out with a one-line private reason. The server
still owns the hard rules and refuses a pick that breaks one (7-day repeat, retired, Hards
before the plan allows, burned pattern, one problem open), stamps the clock, and keeps RULE
ZERO: the handoff carries no pattern, the reason shows only after logging, and any coach text
naming the open problem's pattern is refused until the learner names it. Without a coach the
old slot picker remains as a "default pick" so nobody is stuck.

## Steps

1. **Personal, stable (now).** Cutover from the markdown repo, daily backups, phone-width check.
   Effort: 1-2 days. Done when the old repo is frozen and a week passes cleanly.

2. **Hosted, still single user.** Docker image on Fly with a volume (drafted). Needs a way for
   the local bridge to reach the hosted app: an authenticated API for the bridge tools plus one
   outbound connection from the laptop. Effort: 4-6 days. Risk: the blocking `wait_for_event`
   over the internet needs reconnect handling.

3. **Multi-user.** Sign-up and login, per-user data checks on every query, Postgres and Alembic
   migrations, per-user bridge tokens (a user pastes a token into their bridge config), password
   reset, rate limits, a privacy page. Effort: 8-12 days. Risk: one missed user filter is a data
   leak between users; needs a test that runs two users through every endpoint.

4. **Hosted event relay.** The server holds the event queue and message stream so a user's bridge
   can disconnect and resume without losing events. Replaces DB polling with a push channel.
   Effort: 4-6 days.

5. **leetcode.com browser extension.** Reads the problem the user opened and sends approach, run
   and submit events in the same event shape, so the user works on leetcode.com instead of the
   app's editor. Risks: page changes break it, the site's terms must be read, store review takes
   time. Effort: 8-10 days for Chrome.

6. **In-app Claude API coach (option, BYOK).** For users without Claude Code: the server calls
   the API with the user's own key, using the coaching rules as a cached system prompt, a leak
   guard on every reply, and a spend cap. The first design had this and dropped it to keep cost
   at $0. The earlier estimate was about $25-50 a month for a heavy user on the larger models, so
   bring-your-own-key, not a free tier. Effort: 6-8 days. Risk: the leak guard is the product and
   needs its own test set.

## Order, and what not to build yet
Steps 1, 2, 3 in that order. Step 4 only if disconnects hurt in step 2. Steps 5 and 6 are
independent and can follow 3 in either order; 6 widens the audience most, 5 improves daily use
most. Not planned: payments, teams, social features, other languages.

Total to a usable multi-user product (1-3): about 2-3 weeks. All six: about 7-9 weeks.

## Open questions
Does anyone besides the current user want this? Try it with two or three people after step 3
before building 5 and 6. Is the Claude Code requirement acceptable to them, or is BYOK needed
at once?
