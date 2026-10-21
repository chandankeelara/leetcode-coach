"""Verification gate: the app's numbers must match the OLD scripts on the same day.

    python -m migrate.verify [OLD_REPO] [--no-import]

Runs `python tools/status.py` and `python tools/report.py --date 2026-12-15` in the old repo,
parses what they print, and compares it with coach.service.status()/report() on the migrated
database. Prints a PASS / FAIL table. By default it re-runs the (idempotent) import first so
both sides read the same attempts.csv.

PASS*  = the numbers differ for a reason that is checked in code and printed in the note
         (see the 7-day blocklist: status.py derives it from session-log text, we derive it
         from attempts + handouts). Nothing is hidden: the note says exactly what differs.
Exit code 0 only when nothing FAILS.
"""
import os
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta

from coach import db, rules, service
from migrate.import_old_repo import DEFAULT_OLD, PROB, import_old_repo, parse_day

REPORT_DATE = date(2026, 12, 15)


# ---- run the old scripts -----------------------------------------------------------------------
def run_old(root, script, *args):
    py = shutil.which("python") or sys.executable
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run([py, os.path.join("tools", script), *args], cwd=root, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise SystemExit(f"{script} failed:\n{p.stderr}")
    return p.stdout


def sections(text):
    """Split status.py output by its `=====` banners: title -> [lines]."""
    out, title, lines = {}, None, []
    raw = text.splitlines()
    i = 0
    while i < len(raw):
        if re.fullmatch(r"=+", raw[i].strip() or "x") and i + 2 < len(raw) and re.fullmatch(
                r"=+", raw[i + 2].strip() or "x"):
            if title is not None:
                out[title] = lines
            title, lines = raw[i + 1].strip(), []
            i += 3
            continue
        lines.append(raw[i])
        i += 1
    if title is not None:
        out[title] = lines
    return out


def find(sec, prefix):
    for t, lines in sec.items():
        if t.startswith(prefix):
            return lines
    return []


def grab(lines, pattern, group=1, cast=str):
    for ln in lines:
        m = re.search(pattern, ln)
        if m:
            return cast(m.group(group))
    return None


# ---- the checks --------------------------------------------------------------------------------
class Table:
    def __init__(self):
        self.rows = []

    def add(self, name, old, new, note="", explained=False):
        old, new = str(old), str(new)
        if old == new:
            res = "PASS"
        elif explained:
            res = "PASS*"
        else:
            res = "FAIL"
        self.rows.append((name, old, new, res, note))

    def show(self):
        w0 = max(len(r[0]) for r in self.rows)
        w1 = min(46, max(len(r[1]) for r in self.rows))
        w2 = min(46, max(len(r[2]) for r in self.rows))
        print(f"{'check':<{w0}}  {'old script':<{w1}}  {'coach app':<{w2}}  result")
        print("-" * (w0 + w1 + w2 + 16))
        for name, old, new, res, note in self.rows:
            print(f"{name:<{w0}}  {old[:46]:<{w1}}  {new[:46]:<{w2}}  {res}")
            if note and res != "PASS":
                print(f"{'':<{w0}}    note: {note}")
        bad = [r for r in self.rows if r[3] == "FAIL"]
        star = [r for r in self.rows if r[3] == "PASS*"]
        print("-" * (w0 + w1 + w2 + 16))
        print(f"{len(self.rows)} checks: {len(self.rows) - len(bad) - len(star)} PASS, "
              f"{len(star)} PASS* (explained), {len(bad)} FAIL")
        return not bad


def blocklist_note(root, today, old, new):
    """Explain a blocklist difference from the old repo's own files. Returns (explained, note)."""
    only_old, only_new = sorted(old - new), sorted(new - old)
    sdir = os.path.join(root, "sessions")
    notes, ok = [], True
    for n in only_old:
        # the logs status.py actually blocks on: a table row names n AND the newest date
        # anywhere in the file (filename or any line) is under 7 days old
        why = []
        for name in sorted(os.listdir(sdir)):
            text = open(os.path.join(sdir, name), encoding="utf-8").read()
            rows = [l for l in text.splitlines() if l.lstrip().startswith("|")]
            if not any(int(x) == n for l in rows for x in PROB.findall(l)):
                continue
            fday = parse_day(name)
            newest = max([d for d in (parse_day(l) for l in text.splitlines()) if d] + [fday])
            if (today - newest).days < 7:
                if fday and (today - fday).days >= 7:
                    why.append(f"{name} (file dated {fday}, but a note line mentions {newest})")
                else:
                    why.append(None)
        if not why or None in why:
            ok = False
            notes.append(f"{n}: only in old, no explanation found")
        else:
            notes.append(f"{n}: old status.py blocks it because " + "; ".join(why))
    for n in only_new:
        mentioned = False
        for name in sorted(os.listdir(sdir)):
            text = open(os.path.join(sdir, name), encoding="utf-8").read()
            if any(int(x) == n for l in text.splitlines() if l.lstrip().startswith("|")
                   for x in PROB.findall(l)):
                mentioned = True
        if mentioned:
            ok = False
            notes.append(f"{n}: only in new, but a session log mentions it")
        else:
            notes.append(f"{n}: in attempts.csv within 7 days but no session-log table row yet")
    return ok, " | ".join(notes)


def verify(root=DEFAULT_OLD, do_import=True):
    root = os.path.abspath(root)
    today = date.today()
    if do_import:
        import_old_repo(root)
    uid = service.default_user_id()
    st = service.status(uid, today)
    rep = service.report(uid, REPORT_DATE, today)

    t = Table()
    sec = sections(run_old(root, "status.py"))

    # -- today / backlog
    lines = find(sec, "TODAY")
    b = st["backlog"]
    t.add("backlog", grab(lines, r"BACKLOG\s+(\d+) problems", cast=int), b["backlog"])
    t.add("  days", grab(lines, r"since .*?: (\d+) days x", cast=int), b["days"])
    t.add("  owed before today",
          int(grab(lines, r"(\d+) days x (\d+) owed", 1)) * int(grab(lines, r"(\d+) days x (\d+) owed", 2)),
          b["owed_before"])
    t.add("  done before today", grab(lines, r"owed, (\d+) done", cast=int), b["done_before"])
    t.add("target today", grab(lines, r"TARGET TODAY\s+(\d+)", cast=int), b["target_today"])
    t.add("done today", grab(lines, r"Done today\s+(\d+)", cast=int), b["done_today"])
    t.add("remaining", grab(lines, r"REMAINING\s+(\d+)", cast=int), b["remaining"])

    # -- streak
    lines = find(sec, "THE STREAK")
    sk = st["streak"]
    t.add("streak current", grab(lines, r"Current\s+(\d+) day", cast=int), sk["current"])
    t.add("streak longest", grab(lines, r"Longest ever\s+(\d+) day", cast=int), sk["longest"])
    t.add("streak last 14 days", grab(lines, r"([#.]{14})\s+\(last 14"), sk["last14"])

    # -- tier bars
    lines = find(sec, "TIER 1 PROGRESS")
    old_bars = {}
    for ln in lines:
        m = re.match(r"^\s+(\S+)\s+\[(#*)\.*\]\s+(OWNED|\d+ to go|not probed)\s*$", ln)
        if m:
            old_bars[m.group(1)] = (len(m.group(2)), m.group(3).lower())
    for r in st["tiers"]["tier1"]:
        t.add(f"tier1 {r['pattern']}", "%d clean, %s" % old_bars.get(r["pattern"], (-1, "?")),
              f"{r['clean']} clean, {r['status']}")
    tp = st["tiers"]
    t.add("tier1 clean solves owed", grab(lines, r"still owed on Tier 1:\s+(\d+)", cast=int), tp["owed"])
    t.add("clean rate (status)", grab(lines, r"Clean rate\s+(\d+%)"), f"{tp['clean_rate']:.0%}")
    t.add("attempts needed", grab(lines, r"at that rate\s+~(\d+)", cast=int), tp.get("attempts_needed"))
    t.add("ready at 1/day", grab(lines, r"At 1/day\s+ready ~(\S+)"), tp.get("ready_1_per_day"))
    t.add("ready at 55%", grab(lines, r"55% clean rate\s+ready ~(\S+)"), tp.get("ready_at_55"))
    t.add("tier 2 open", grab(lines, r"Tier 2 still open: (.*)"), ", ".join(tp["tier2_open"]) or "none")

    # -- weakest / strongest
    weak = [l for l in find(sec, "WEAKEST") if re.match(r"^\s+\[\d\] ", l)]
    for i, k in enumerate(st["weakest"]):
        want = f"  [{k['level']}] {k['name']}"
        got = weak[i] if i < len(weak) else ""
        t.add(f"weakest #{i + 1}", got[:len(want)].strip(), want.strip())
    strong = [l for l in find(sec, "STRENGTHS") if re.match(r"^\s+\[\d\] ", l)]
    t.add("strengths count", len(strong), len(st["strongest"]))
    for i, k in enumerate(st["strongest"]):
        want = f"  [{k['level']}] {k['name']}"
        got = strong[i] if i < len(strong) else ""
        t.add(f"strength #{i + 1}", got[:len(want)].strip(), want.strip())

    # -- retry queue: DUE/wait flags in file order
    old_retry = [(int(m.group(2)), m.group(1).strip()) for l in find(sec, "RETRY QUEUE")
                 for m in [re.match(r"^\s+(DUE|wait)\s+(\d+)\.", l)] if m]
    new_retry = [(r["problem_number"], "DUE" if r["due"] else "wait") for r in st["retry"]]
    t.add("retry queue (problem, flag)", old_retry, new_retry)

    # -- re-fire
    old_rf = [(m.group(2), m.group(1).strip(), int(m.group(3)), int(m.group(4)))
              for l in find(sec, "RE-FIRE")
              for m in [re.match(r"^\s+(DUE|wait)\s+(\S+)\s+(\d+)d since last touch\s+\((\d+)/3", l)] if m]
    new_rf = [(r["pattern"], "DUE" if r["due"] else "wait", r["age"], r["refires_done"])
              for r in st["refire"]]
    t.add("re-fire (pattern, flag, age, done)", old_rf, new_rf)

    # -- 7-day blocklist
    bl = find(sec, "7-DAY BLOCKLIST")
    text = next((l.strip() for l in bl if l.strip()), "")
    old_set = set() if text == "nothing blocked" else {int(x) for x in text.split(",")}
    new_set = set(st["blocklist"])
    explained, note = (True, "") if old_set == new_set else blocklist_note(root, today, old_set, new_set)
    t.add("7-day blocklist", sorted(old_set), sorted(new_set), note, explained)

    # -- report.py
    r_out = run_old(root, "report.py", "--date", REPORT_DATE.isoformat())
    tput, q, cov, pr = rep["throughput"], rep["quality"], rep["coverage"], rep["projection"]
    L = r_out.splitlines()
    t.add("report: window", grab(L, r"Window\s+(\S+ to \S+)\s+\((\d+) days\)"),
          f"{tput['first']} to {today.isoformat()}")
    t.add("report: days elapsed", grab(L, r"Window .*\((\d+) days\)", cast=int), tput["elapsed"])
    t.add("report: days practised", grab(L, r"Days practised\s+(\d+) of", cast=int), tput["active"])
    t.add("report: consistency", grab(L, r"\((\d+)% consistency\)") + "%", f"{tput['consistency']:.0%}")
    t.add("report: days missed", grab(L, r"Days missed\s+(\d+)", cast=int), tput["missed"])
    t.add("problems completed", grab(L, r"Problems completed\s+(\d+)", cast=int), tput["completed"])
    t.add("problems abandoned", grab(L, r"Problems abandoned\s+(\d+)", cast=int), tput["abandoned"])
    t.add("rate per calendar day", grab(L, r"CALENDAR day\s+(\S+)"), f"{tput['rate_calendar']:.2f}")
    t.add("rate per active day", grab(L, r"ACTIVE day\s+(\S+)"), f"{tput['rate_active']:.2f}")
    old_blocks = [m.groups() for l in L
                  for m in [re.match(r"^\s+(\d\d-\d\d-\d\d-\d\d)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\d+/\d+)\s*$", l)] if m]
    new_blocks = [(b_["span"], str(b_["solves"]), f"{b_['bugs_per_solve']:.2f}",
                   f"{b_['time_vs_target']:.2f}", f"{b_['clean']}/{b_['solves']}") for b_ in q["blocks"]]
    t.add("quality blocks", len(old_blocks), len(new_blocks))
    for i, (o, n) in enumerate(zip(old_blocks, new_blocks)):
        t.add(f"  block {i + 1} {o[0]}", " ".join(o[1:]), " ".join(n[1:]))
    t.add("first 6 / last 6 bugs", "%s / %s" % (grab(L, r"First 6 solves: (\S+) bugs"),
                                              grab(L, r"Last 6: (\S+) bugs")),
          f"{q['first6_bugs']:.2f} / {q['last6_bugs']:.2f}")
    t.add("coverage owned", grab(L, r"Owned\s+(\d+) /", cast=int), len(cov["owned"]))
    t.add("coverage started", grab(L, r"Started\s+(\d+) /", cast=int), len(cov["started"]))
    t.add("coverage untouched", grab(L, r"Untouched\s+(\d+) /", cast=int), len(cov["untouched"]))
    old_cc = {m.group(1): (int(m.group(2)), int(m.group(3))) for l in L
              for m in [re.match(r"^\s{4}(\S+)\s+(\d+) clean / (\d+) attempts", l)] if m}
    new_cc = {p: (c["clean"], c["attempts"]) for p, c in cov["counts"].items() if c["attempts"]}
    t.add("coverage clean/attempts per pattern", dict(sorted(old_cc.items())), dict(sorted(new_cc.items())))
    t.add("proj: tier1 owed", grab(L, r"owed on Tier 1\s+(\d+)", cast=int), pr["tier1_owed"])
    t.add("proj: clean rate", grab(L, r"hit rate\s+(\d+%)"), f"{pr['clean_ratio']:.0%}")
    t.add("proj: tier1 attempts", grab(L, r"clean rate\s+~(\d+)", cast=int), pr["tier1_attempts"])
    t.add("proj: tier1 ready 1/2/3 per day",
          [grab(L, rf"At {n}/day\s+(\d{{4}}-\d\d-\d\d)") for n in (1, 2, 3)],
          [pr["tier1_ready"][f"{n}/day"] for n in (1, 2, 3)])
    t.add("proj: tier1+2 owed", grab(L, r"still owed\s+(\d+)", cast=int), pr["owed"])
    t.add("proj: problems to attempt", grab(L, r"to attempt\s+~(\d+)", cast=int), pr["problems_to_attempt"])
    t.add("proj: days at pace", grab(L, r"current \S+/day\s+(\d+) days"), f"{pr['days_at_pace']:.0f}")
    t.add("proj: ready around", grab(L, r"Ready around\s+(\S+)"), pr["ready_around"])
    t.add("proj: need rate", grab(L, r"you need\s+(\S+) problems/day"), f"{pr['need_rate']:.2f}")
    t.add("proj: delta vs today", grab(L, r"That is (\S+)/day versus today"), f"{pr['need_rate_delta']:+.2f}")
    for label, w in pr["week"].items():
        t.add(f"proj: {label}", grab(L, rf"{label}\s+-> (\S+)/day\s+-> ready in (\S+) weeks", 1) + " / " +
              grab(L, rf"{label}\s+-> (\S+)/day\s+-> ready in (\S+) weeks", 2),
              f"{w['rate']:.2f} / {w['weeks']:.1f}")

    ok = t.show()
    print(f"\nverified on {today} against {root}")
    return ok


def main(argv=None):
    args = [a for a in (argv if argv is not None else sys.argv[1:])]
    do_import = "--no-import" not in args
    paths = [a for a in args if not a.startswith("--")]
    db.init()
    ok = verify(paths[0] if paths else DEFAULT_OLD, do_import)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
