"""Command line for Claude Code and for you.

    python -m coach.cli create-user NAME        # password prompt, or env COACH_PASSWORD
    python -m coach.cli migrate [OLD_REPO] [--force]
    python -m coach.cli verify
    python -m coach.cli status [--json]
    python -m coach.cli next [--json] [--new]
    python -m coach.cli log --handout 12 --min 14 --bugs 0 [--hints 1] [--no-optimal]
                            [--no-cold] [--abandoned] [--notes TEXT]
    python -m coach.cli reveal HANDOUT_ID
    python -m coach.cli coach-token NAME [--label L]   # prints a new coach token ONCE
    python -m coach.cli coach-token NAME --revoke ID
    python -m coach.cli coach-token NAME --list

DB path: env COACH_DB, default ./coach.db. The user is the first (only) user.
RULE ZERO: `next` prints the Handoff fields and nothing else. `status` is progress talk; never
print it next to a handoff.
"""
import argparse
import getpass
import json
import os
import sys

from . import db, service


def _user():
    uid = service.default_user_id()
    if uid is None:
        sys.exit("no user yet: run `python -m coach.cli create-user NAME` (or migrate)")
    return uid


def _bar(clean, need):
    return "#" * clean + "." * need


def render_status(st):
    b, sk, t = st["backlog"], st["streak"], st["tiers"]
    out = [f"STATUS {st['today']}", ""]
    out.append(f"  BACKLOG        {b['backlog']} problems  ({b['days']} days, "
               f"{b['owed_before']} owed, {b['done_before']} done)")
    out.append(f"  TARGET TODAY   {b['target_today']}")
    out.append(f"  DONE TODAY     {b['done_today']}")
    out.append(f"  REMAINING      {b['remaining']}")
    out.append(f"  STREAK         {sk['current']} current, {sk['longest']} longest   {sk['last14']}")
    out.append("")
    out.append("  TIER 1")
    for r in t["tier1"]:
        out.append(f"    {r['pattern']:<22} [{_bar(r['clean'], r['needed'])}]  {r['status'].upper()}")
    out.append(f"  Clean solves owed on Tier 1: {t['owed']}   clean rate {t['clean_rate']:.0%}")
    out.append(f"  Tier 2 open: {', '.join(t['tier2_open']) or 'none'}")
    out.append("")
    out.append("  RETRY QUEUE")
    for r in st["retry"] or []:
        out.append(f"    {'DUE ' if r['due'] else 'wait'} {r['problem_number']}. {r.get('title', '')}"
                   f"  ({r['age']}d, due at {r['wait']}d)")
    if not st["retry"]:
        out.append("    empty")
    out.append("  RE-FIRE")
    for r in st["refire"]:
        out.append(f"    {'DUE ' if r['due'] else 'wait'} {r['pattern']:<22} {r['age']}d  "
                   f"({r['refires_done']}/3 done)")
    if not st["refire"]:
        out.append("    nothing retired yet")
    out.append("  7-DAY BLOCKLIST  " + (", ".join(map(str, st["blocklist"])) or "nothing blocked"))
    out.append("  WEAKEST")
    for k in st["weakest"]:
        out.append(f"    [{k['level']}] {k['name']}  {k['trend']}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="coach")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create-user")
    p.add_argument("name")
    p = sub.add_parser("migrate")
    p.add_argument("repo", nargs="?", default=None)
    p.add_argument("--user", default="chandan")
    p.add_argument("--force", action="store_true")
    sub.add_parser("verify")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("next")
    p.add_argument("--json", action="store_true")
    p.add_argument("--new", action="store_true", help="skip any open handout and pick again")
    p = sub.add_parser("log")
    p.add_argument("--handout", type=int)
    p.add_argument("--number", type=int)
    p.add_argument("--min", type=int, required=True, dest="actual")
    p.add_argument("--bugs", type=int, default=0)
    p.add_argument("--hints", type=int, default=0)
    p.add_argument("--optimal", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--cold", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--abandoned", action="store_true")
    p.add_argument("--notes", default="")
    p = sub.add_parser("reveal")
    p.add_argument("handout_id", type=int)
    p = sub.add_parser("ext-token", help="print the browser-extension token")
    p.add_argument("--rotate", action="store_true")
    p = sub.add_parser("coach-token", help="coach tokens for /mcp and /tools")
    p.add_argument("name")
    p.add_argument("--label", default="cli")
    p.add_argument("--revoke", type=int, metavar="ID")
    p.add_argument("--list", action="store_true")
    args = ap.parse_args(argv)

    db.init()
    if args.cmd == "create-user":
        pw = os.environ.get("COACH_PASSWORD") or getpass.getpass("password: ")
        if not os.environ.get("COACH_PASSWORD") and getpass.getpass("again: ") != pw:
            sys.exit("passwords differ")
        uid = service.create_user(args.name, pw)
        print(f"user {args.name} id {uid}")
    elif args.cmd == "migrate":
        from migrate.import_old_repo import DEFAULT_OLD, import_old_repo
        rep = import_old_repo(args.repo or DEFAULT_OLD, args.user, args.force)
        from . import plans
        with db.session_scope() as s:
            rep["plans_created"] = plans.migrate_legacy(s)
        for k, v in rep.items():
            print(f"  {k}: {len(v) if isinstance(v, list) else v}")
    elif args.cmd == "verify":
        from migrate.verify import main as verify_main
        sys.exit(verify_main([]))
    elif args.cmd == "status":
        st = service.status(_user())
        print(json.dumps(st, indent=2, default=str) if args.json else render_status(st))
    elif args.cmd == "next":
        h = service.next_problem(_user(), force_new=args.new)
        if h is None:
            sys.exit("nothing passes the filters")
        if args.json:
            print(h.model_dump_json(indent=2))
        else:
            print(f"{h.number}. {h.title}\n{h.leetcode_url}")
            if h.paraphrase:
                print(h.paraphrase)
            print(f"start {h.started_at:%H:%M}   target {h.target_min} min   (handout {h.handout_id})")
            print(h.instruction)
    elif args.cmd == "log":
        if args.handout is None and args.number is None:
            sys.exit("give --handout or --number")
        res = service.log_attempt(
            _user(), handout_id=args.handout, number=args.number, actual_min=args.actual,
            bugs=args.bugs, optimal=args.optimal, derived_cold=args.cold, hints=args.hints,
            notes=args.notes, abandoned=args.abandoned)
        print(json.dumps(res))
    elif args.cmd == "ext-token":
        from . import ext
        print(ext.ext_token_rotate() if args.rotate else ext.ext_token_get_or_create())
    elif args.cmd == "coach-token":
        from . import tokens
        uid = tokens.user_id_by_name(args.name)
        if uid is None:
            sys.exit(f"no user {args.name!r}")
        if args.revoke is not None:
            try:
                live = tokens.revoke(uid, args.revoke)
            except ValueError as e:
                sys.exit(str(e))
            print(f"token {args.revoke} " + ("revoked" if live else "was already revoked"))
        elif args.list:
            for t in tokens.list_for(uid):
                state = "revoked" if t["revoked_at"] else "live"
                print(f"{t['id']:>4}  {state:<8} {t['label']:<24} created {t['created_at']:%Y-%m-%d %H:%M}"
                      f"  last used {t['last_used_at'] or 'never'}")
        else:
            t = tokens.create(uid, args.label)
            print(t["token"])
            print(f"(token id {t['id']}, label {t['label']!r}; shown once, stored hashed)",
                  file=sys.stderr)
    elif args.cmd == "reveal":
        print(json.dumps(service.reveal(args.handout_id), indent=2))


if __name__ == "__main__":
    main()
