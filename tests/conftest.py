"""Shared fixtures: a throwaway database and a small synthetic problem bank."""
import csv
import os
from datetime import date, datetime

import pytest

from coach import db, rules, service
from coach.catalog import lc_slug, pattern_name, pattern_tier
from coach.models import Pattern, Problem, Skill

HERE = os.path.dirname(__file__)
TODAY = date(2026, 10, 21)


def load_attempts_csv():
    """Frozen copy of the old repo's attempts.csv (as of 2026-10-21), as plain dicts."""
    with open(os.path.join(HERE, "fixtures", "attempts.csv"), encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["day"] = datetime.strptime(r["date"], "%Y-%m-%d").date()
        for k in ("target_min", "actual_min", "bugs"):
            r[k] = int(r[k])
    return sorted(rows, key=lambda r: r["day"])


@pytest.fixture
def csv_attempts():
    return load_attempts_csv()


@pytest.fixture
def world(tmp_path):
    """Fresh DB with one user, every pattern, and 3 problems (E, M, H) per Tier-1/Tier-2
    pattern. Skills: level 4 for the first two Tier-1 patterns, 2 for the rest."""
    db.init(str(tmp_path / "t.db"))
    uid = service.create_user("tester", "pw-for-tests")
    patterns = rules.TIER1 + rules.TIER2
    order = 0
    with db.session_scope() as s:
        for slug in patterns:
            s.add(Pattern(slug=slug, name=pattern_name(slug), tier=pattern_tier(slug)))
        s.flush()
        for i, slug in enumerate(patterns):
            for j, d in enumerate("EMH"):
                order += 1
                n = 1000 + i * 10 + j
                title = f"{slug} problem {d}"
                s.add(Problem(number=n, title=title, lc_slug=lc_slug(n, title), difficulty=d,
                              pattern_slug=slug, source="bank", bank_group=slug,
                              bank_order=order))
        for i, slug in enumerate(rules.TIER1):
            s.add(Skill(user_id=uid, name=pattern_name(slug), kind="pattern", pattern_slug=slug,
                        level=4 if i < 2 else 2, trend="", evidence=""))
    return uid
