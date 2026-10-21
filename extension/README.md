# LeetCode Coach Link (Chrome / Edge, Manifest V3)

Sends each submission you make on leetcode.com to your local coaching app, so the coach sees it
the same way it sees a submission from the app's own editor.

## Install
1. Start the coach web app (default http://127.0.0.1:8731).
2. Chrome/Edge: open `chrome://extensions`, turn on Developer mode, "Load unpacked", pick this
   `extension/` folder.
3. Get the token: `python -m coach.cli ext-token` (`--rotate` makes a new one).
4. Click the extension icon, paste the token, keep or change the server URL, press
   "Test connection". It shows the number of the currently open problem.

## Behavior
- On `leetcode.com/problems/*`, after you click Submit, it watches for a verdict (Accepted,
  Wrong Answer, Time Limit Exceeded, Memory Limit Exceeded, Output Limit Exceeded, Runtime Error,
  Compile Error), reads your code from the Monaco editor, the language from the selector, and the
  slug from the URL, then POSTs to `/api/ext/submission`. One submission is sent once; "Run" never
  sends.
- The app only accepts it when the slug is the problem the coach currently has open. Otherwise
  nothing is pushed.
- A small dot bottom-left of the page: green = sent, or the open problem matches this page;
  grey = idle or not the open problem; red = no connection or bad token.

## Never sent
Problem statement text, examples, editorials, comments, cookies, or your leetcode account. Only:
slug, code, language, verdict, and runtime/memory text if visible.

## Caveats
LeetCode changes its DOM often. Detection uses visible text, not class names, but may need
tweaks. If Monaco is not reachable it falls back to visible editor lines (may be truncated).
