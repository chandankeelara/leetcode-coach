"""Import the old markdown/CSV coaching repo into the coach database. Read-only on the old repo.

    python -m migrate.import_old_repo [OLD_REPO] [--user chandan] [--force]

Idempotent: every run WIPES the imported tables (attempts, handouts, sessions, skills, retry
items, pattern state, oa/trap rows, problems, patterns, config) and reloads them, so it can be
re-run until cutover. It refuses to run once the app has logged attempts of its own
(source = "app") unless --force, because the wipe would destroy them.

Mapping (design doc section 10):
  data/attempts.csv         -> attempts, problems
  PROBLEM_BANK.md           -> problems (bank_group, bank_order) + the reproduction list
  SKILL_LEDGER.md table     -> skills + one skill_event per row
  RETRY_QUEUE.md            -> retry_items
  sessions/*.md             -> sessions.log_md verbatim; "handed out" rows -> handouts
  patterns/*.md             -> pattern_state (Retired / Re-fired dates, note text)
  data/oa_sims.csv, data/trap_recall.csv -> oa_sims, trap_recall
  tools/status.py constants -> config
"""
import argparse
import csv
import importlib.util
import os
import re
import sys
from datetime import date, datetime

from sqlalchemy import delete, select, update

from coach import db, rules
from coach.catalog import lc_slug, pattern_name, pattern_tier
from coach.models import (Attempt, Burned, CoachMessage, CoachSession, Config, Handout, Pattern,
                          PatternState, Problem, RetryItem, Skill, SkillEvent, TrapRecall, OaSim,
                          User)

DEFAULT_OLD = r"D:\Git\virtual457-projects\leetcode"
PROB = re.compile(r"\b(\d{1,4})\.\s+[A-Za-z]")

# PROBLEM_BANK.md group heading -> default pattern slug
BANK_GROUP = {
    "Monotonic stack / deque": "monotonic-stack",
    "Grid BFS / DFS": "grid-bfs",
    "Prefix sum / hashmap": "prefix-hashmap",
    "Binary search on answer": "binary-search-answer",
    "Sliding window (non-negative)": "sliding-window",
    "Two pointers": "two-pointers",
    "Trees / DFS": "trees-dfs",
    "Linked list": "linked-list",
    "Heap": "heap",
    "Intervals": "intervals",
    "Backtracking": "backtracking",
    "DP 1D": "dp-1d",
    "DP 2D": "dp-2d",
    "Graphs (non-grid)": "graph-toposort",
    "Trie": "trie",
}
# reproduction-list group -> default pattern slug
REPRO_GROUP = {
    "Monotonic deque": "monotonic-deque", "Heap": "heap", "Linked list": "linked-list",
    "Trees DFS": "trees-dfs", "Trees BFS": "trees-bfs", "Two pointers": "two-pointers",
    "Sliding window": "sliding-window", "Hashing": "hashing", "Prefix / suffix": "prefix-hashmap",
    "Binary search (array)": "binary-search-array", "Stack": "stack", "Intervals": "intervals",
    "Greedy / running state": "greedy", "Backtracking": "backtracking", "DP 1D": "dp-1d",
    "DP 2D": "dp-2d", "Graphs": "graphs", "Trie": "trie", "Matrix": "matrix",
    "Bit manipulation": "bit-manipulation", "Index as hash": "index-as-hash",
}
# problems whose pattern differs from their group's default
PATTERN_OVERRIDE = {
    239: "monotonic-deque", 862: "monotonic-deque", 102: "trees-bfs",
    323: "union-find", 684: "union-find", 743: "graph-shortest-path",
    200: "grid-bfs", 207: "graph-toposort",
}
# SKILL_LEDGER row name -> pattern slug (the rest are meta skills)
SKILL_PATTERN = {
    "Two pointers": "two-pointers", "Trees / DFS": "trees-dfs", "Trees / BFS": "trees-bfs",
    "Sliding window": "sliding-window", "Intervals": "intervals",
    "Binary search (array form)": "binary-search-array", "Heap / top-K": "heap",
    "Linked list": "linked-list", "Monotonic deque (window form)": "monotonic-deque",
    "Grid BFS": "grid-bfs", "Binary search on answer": "binary-search-answer",
    "Prefix sum": "prefix-hashmap", "Monotonic stack (stack form)": "monotonic-stack",
}


# ---- parsing helpers (same logic as status.py) -----------------------------------------------
def read(root, name):
    path = os.path.join(root, name)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def md_rows(markdown, min_cols):
    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < min_cols:
            continue
        if set(cells[0]) <= set("-: ") or not cells[0]:
            continue
        yield cells


def parse_day(text):
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    return date(*map(int, m.groups())) if m else None


def _num(text):
    try:
        return float(text)
    except ValueError:
        return None


def _int(text):
    n = _num(text)
    return None if n is None else int(n)


def load_constants(root):
    """Constants from the old tools/status.py, loaded as a module (it only defines names at
    import time) so the config matches the real source."""
    path = os.path.join(root, "tools", "status.py")
    spec = importlib.util.spec_from_file_location("old_status", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {
        "backlog_start": mod.BACKLOG_START.isoformat(), "per_day": mod.PROBLEMS_PER_DAY,
        "ladder": list(mod.LADDER), "clean_to_own": mod.CLEAN_TO_OWN,
        "daily_min": mod.DAILY_MIN, "target_min": mod.TARGET_MIN,
        "tier1": list(mod.TIER1), "tier2": list(mod.TIER2),
        "hards_from": rules.HARDS_FROM.isoformat(),      # not in status.py: from the design doc
        "block_days": 7, "refire_days": 7,
    }


# ---- the importer -------------------------------------------------------------------------------
class Importer:
    def __init__(self, session, root, username):
        self.s = session
        self.root = root
        self.username = username
        self.problems = {}      # number -> dict
        self.report = {}        # counts + notes, returned to the caller

    # -- problems ------------------------------------------------------------------------------
    def _put_problem(self, number, title, diff, pattern, source, group=None, order=None,
                     repro=False):
        cur = self.problems.get(number)
        if cur is None:
            self.problems[number] = {
                "title": title or "", "difficulty": diff or "M", "pattern": pattern,
                "source": source, "group": group, "order": order, "repro": repro}
            return
        # bank > reproduction > attempt: fill gaps, never overwrite a better source
        if title and (not cur["title"] or (cur["source"] == "attempt" and source != "attempt")):
            cur["title"] = title
        if source != "attempt" and cur["source"] == "attempt":
            cur["source"], cur["group"] = source, group
            if pattern:
                cur["pattern"] = pattern
        if cur["order"] is None and order is not None:
            cur["order"] = order
        if diff and source != "attempt":
            cur["difficulty"] = diff
        if not cur["pattern"] and pattern:
            cur["pattern"] = pattern
        cur["repro"] = cur["repro"] or repro

    def read_bank(self):
        text = read(self.root, "PROBLEM_BANK.md")
        group, order, in_repro, repro_group = None, 0, False, None
        item = re.compile(r"\[[ xr]\]\s+(\d+)\s*(.*?)\s*\(([EMH])\b")
        for line in text.splitlines():
            if line.startswith("## "):
                head = re.split(r"\s+[—-]\s+|\s{2,}", line[3:].strip())[0].strip()
                in_repro = head.startswith("Reproduction list")
                group = head
                continue
            if not in_repro:
                m = re.match(r"^- \[[ xr]\]\s+(\d+)\s+(.+?)\s+\(([EMH])\)", line)
                if m and group:
                    n = int(m.group(1))
                    order += 1
                    pat = PATTERN_OVERRIDE.get(n) or BANK_GROUP.get(group)
                    self._put_problem(n, m.group(2), m.group(3), pat, "bank", group, order)
                continue
            m = re.match(r"^- \*\*(.+?):\*\*(.*)$", line)
            if not m:
                continue
            repro_group = m.group(1).strip()
            for part in m.group(2).split("·"):
                im = item.search(part)
                if not im:
                    continue
                n = int(im.group(1))
                order += 1
                pat = PATTERN_OVERRIDE.get(n) or REPRO_GROUP.get(repro_group)
                self._put_problem(n, im.group(2), im.group(3), pat, "reproduction",
                                  repro_group, order, repro=True)

    def read_attempts(self):
        path = os.path.join(self.root, "data", "attempts.csv")
        with open(path, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        for r in rows:
            self._put_problem(int(r["problem"]), r["name"], r["difficulty"], r["pattern"], "attempt")
        return rows

    # -- load -------------------------------------------------------------------------------------
    def wipe(self, force):
        s = self.s
        app = s.scalars(select(Attempt.id).where(Attempt.source == "app")).first()
        if app and not force:
            raise SystemExit("the app has logged attempts of its own; a re-import would destroy "
                             "them. Pass --force to wipe anyway.")
        s.execute(delete(Burned))
        s.execute(update(CoachMessage).values(handout_id=None))
        for model in (Attempt, Handout, SkillEvent, Skill, RetryItem, PatternState, OaSim,
                      TrapRecall, CoachSession, Problem, Pattern, Config):
            s.execute(delete(model))
        s.flush()

    def run(self, force=False):
        s = self.s
        user = s.scalars(select(User).where(User.username == self.username)).first()
        if user is None:
            user = User(username=self.username, password_hash="!")
            s.add(user)
            s.flush()
        uid = user.id
        self.wipe(force)

        self.read_bank()
        att_rows = self.read_attempts()
        const = load_constants(self.root)
        for k, v in const.items():
            s.add(Config(key=k, value=v))

        # retry queue titles may add problems the other files do not know
        retry_rows = [c for c in md_rows(read(self.root, "RETRY_QUEUE.md"), 4)
                      if not c[0].lower().startswith("problem")]
        for c in retry_rows:
            m = re.match(r"(\d+)\.\s*(.*)", c[0])
            if m:
                self._put_problem(int(m.group(1)), m.group(2), "M", None, "attempt")

        # patterns: the two tiers, plus anything else a problem or attempt names
        slugs = set(rules.TIER1) | set(rules.TIER2)
        slugs |= {p["pattern"] for p in self.problems.values() if p["pattern"]}
        slugs |= {r["pattern"] for r in att_rows}
        slugs |= set(SKILL_PATTERN.values())
        for slug in sorted(slugs):
            s.add(Pattern(slug=slug, name=pattern_name(slug), tier=pattern_tier(slug)))
        s.flush()

        for n, p in sorted(self.problems.items()):
            s.add(Problem(number=n, title=p["title"] or f"Problem {n}",
                          lc_slug=lc_slug(n, p["title"] or f"problem {n}"),
                          difficulty=p["difficulty"], pattern_slug=p["pattern"],
                          source=p["source"], bank_group=p["group"], bank_order=p["order"],
                          reproduction=p["repro"]))
        s.flush()

        for r in att_rows:
            s.add(Attempt(
                user_id=uid, problem_number=int(r["problem"]), pattern_slug=r["pattern"],
                day=date.fromisoformat(r["date"]), target_min=int(r["target_min"]),
                actual_min=int(r["actual_min"]), bugs=int(r["bugs"]), hints=0, optimal=True,
                derived_cold=True, outcome=r["outcome"], notes="", source="import"))
        self.report["attempts"] = len(att_rows)
        self.report["problems"] = len(self.problems)

        self.read_skills(uid)
        self.read_retry(uid, retry_rows)
        self.read_patterns(uid)
        self.read_sessions(uid, att_rows)
        self.read_csvs(uid)
        s.flush()
        return self.report

    def read_skills(self, uid):
        text = read(self.root, "SKILL_LEDGER.md")
        updated = parse_day(re.search(r"Last updated:.*", text).group(0)) if re.search(
            r"Last updated:.*", text) else None
        n = 0
        for cells in md_rows(text, 4):
            lvl = cells[1].strip("*")
            if not lvl.isdigit():
                continue
            name = cells[0].strip("*").strip()
            slug = SKILL_PATTERN.get(name)
            sk = Skill(user_id=uid, name=name, kind="pattern" if slug else "meta",
                       pattern_slug=slug, level=int(lvl), trend=cells[3].strip("*"),
                       evidence=cells[2].replace("**", ""))
            self.s.add(sk)
            self.s.flush()
            self.s.add(SkillEvent(skill_id=sk.id, day=updated, level=int(lvl), evidence=sk.evidence))
            n += 1
        self.report["skills"] = n

    def read_retry(self, uid, rows):
        n = 0
        for c in rows:
            m = re.match(r"(\d+)\.", c[0])
            day = parse_day(c[1])
            if not m or day is None:
                self.report.setdefault("warnings", []).append(f"retry row skipped: {c[0][:40]}")
                continue
            attempts = _int(c[3]) or 1
            self.s.add(RetryItem(user_id=uid, problem_number=int(m.group(1)), added=day,
                                 last_on=day, reason=c[2], attempts=attempts))
            n += 1
        self.report["retry_items"] = n

    def read_patterns(self, uid):
        pdir = os.path.join(self.root, "patterns")
        n = 0
        for name in sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []:
            if not name.endswith(".md") or name == "README.md":
                continue
            text = open(os.path.join(pdir, name), encoding="utf-8").read()
            m = re.search(r"Retired (\d{4}-\d{2}-\d{2})", text)
            refires = sorted(re.findall(r"Re-fired (\d{4}-\d{2}-\d{2})", text))
            self.s.add(PatternState(
                user_id=uid, pattern_slug=name[:-3],
                retired_on=date.fromisoformat(m.group(1)) if m else None,
                refires=refires, note_md=text))
            n += 1
        self.report["pattern_state"] = n

    def read_sessions(self, uid, att_rows):
        sdir = os.path.join(self.root, "sessions")
        attempted_on = {(r["date"], int(r["problem"])) for r in att_rows}
        attempted = {int(r["problem"]) for r in att_rows}
        n, handouts, unmatched = 0, 0, []
        for name in sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []:
            if not name.endswith(".md"):
                continue
            day = parse_day(name)
            text = open(os.path.join(sdir, name), encoding="utf-8", newline="").read()
            sess = CoachSession(user_id=uid, day=day, label=name[:-3], log_md=text)
            self.s.add(sess)
            self.s.flush()
            n += 1
            for line in text.splitlines():
                if not line.lstrip().startswith("|"):
                    continue
                m = PROB.search(line)
                if not m:
                    continue
                num = int(m.group(1))
                if "handed out" in line and num in self.problems:
                    t = re.search(r"handed out\s+(\d{1,2}):(\d{2})", line)
                    at = datetime.combine(day, datetime.min.time().replace(
                        hour=int(t.group(1)), minute=int(t.group(2)))) if t else datetime.combine(
                        day, datetime.min.time())
                    p = self.problems[num]
                    self.s.add(Handout(user_id=uid, session_id=sess.id, problem_number=num,
                                       slot="imported", pattern_slug=p["pattern"], handed_at=at,
                                       target_min=35 if p["difficulty"] == "H" else 15,
                                       status="skipped"))
                    handouts += 1
                elif (day.isoformat(), num) not in attempted_on and num not in attempted:
                    unmatched.append(f"{name}: {num}")
        self.report["sessions"] = n
        self.report["handouts"] = handouts
        # table rows naming a problem that has no attempt anywhere (informational)
        self.report["session_rows_without_attempt"] = unmatched

    def read_csvs(self, uid):
        n = 0
        path = os.path.join(self.root, "data", "oa_sims.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    self.s.add(OaSim(
                        user_id=uid, day=date.fromisoformat(r["date"]), format=r["format"],
                        problem=r["problem"], score=_num(r["score"]), max_score=_num(r["max"]),
                        minutes=_int(r["minutes"]), read_all_first=r["read_all_first"],
                        bailed_to_brute=r["bailed_to_brute"], lookups=_int(r["lookups"]),
                        notes=r["notes"]))
                    n += 1
        self.report["oa_sims"] = n
        n = 0
        path = os.path.join(self.root, "data", "trap_recall.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    self.s.add(TrapRecall(
                        user_id=uid, day=date.fromisoformat(r["date"]), pattern=r["pattern"],
                        trap=r["trap"], cue=r["cue"], recalled=r["recalled"],
                        hit_in_code=r["hit_in_code"]))
                    n += 1
        self.report["trap_recall"] = n


def import_old_repo(root=DEFAULT_OLD, username="chandan", force=False):
    """Run the import into the current coach database. Returns a report dict."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise SystemExit(f"old repo not found: {root}")
    with db.session_scope() as s:
        return Importer(s, root, username).run(force)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("repo", nargs="?", default=DEFAULT_OLD)
    ap.add_argument("--user", default="chandan")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    rep = import_old_repo(args.repo, args.user, args.force)
    for k, v in rep.items():
        if isinstance(v, list):
            print(f"  {k}: {len(v)}")
            for x in v[:20]:
                print(f"      {x}")
        else:
            print(f"  {k}: {v}")


if __name__ == "__main__":
    sys.exit(main())
