"""Write .mcp.json at the repo root with this machine's absolute paths.

    python -m bridge.install [--user NAME] [--db PATH] [--print-only]

Also prints the `claude mcp add` one-liner that does the same thing without the file.
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Hard wall-clock limit per tool call, ms. wait_for_event defaults to 25 min; give it 30.
# (Claude Code's stdio idle timeout is also 30 min; the server sends progress every 30 s.)
TOOL_TIMEOUT_MS = 30 * 60 * 1000


def python_exe():
    win = REPO / ".venv" / "Scripts" / "python.exe"
    nix = REPO / ".venv" / "bin" / "python"
    return win if win.exists() else (nix if nix.exists() else Path(sys.executable))


def server_entry(user, db):
    env = {"COACH_DB": str(db), "PYTHONPATH": str(REPO)}
    if user:
        env["COACH_USER"] = user
    return {
        "type": "stdio",
        "command": str(python_exe()),
        "args": ["-m", "bridge.server"],
        "env": env,
        "timeout": TOOL_TIMEOUT_MS,
        "alwaysLoad": True,
    }


def one_liner(entry):
    envs = " ".join(f'-e {k}="{v}"' for k, v in entry["env"].items())
    return (f'claude mcp add -s user {envs} --transport stdio coach -- '
            f'"{entry["command"]}" -m bridge.server')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=os.environ.get("COACH_USER", ""))
    ap.add_argument("--db", default=os.environ.get("COACH_DB") or str(REPO / "coach.db"))
    ap.add_argument("--print-only", action="store_true")
    a = ap.parse_args(argv)
    db = Path(a.db).resolve()
    entry = server_entry(a.user, db)
    cfg = {"mcpServers": {"coach": entry}}
    text = json.dumps(cfg, indent=2) + "\n"
    if a.print_only:
        print(text)
    else:
        (REPO / ".mcp.json").write_text(text, encoding="utf-8")
        print(f"wrote {REPO / '.mcp.json'}")
    print("\nOr register it for every project instead of the file:\n")
    print("  " + one_liner(entry))
    print("\n(The one-liner sets no per-server timeout; Claude Code's default MCP_TOOL_TIMEOUT"
          " is far above 25 min, so the long wait still survives. If you have lowered it,"
          f" set MCP_TOOL_TIMEOUT={TOOL_TIMEOUT_MS}.)")


if __name__ == "__main__":
    main()
