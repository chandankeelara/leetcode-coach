"""In-memory stand-in with the same methods as coach.web.svc.Svc."""
from datetime import datetime
from types import SimpleNamespace


class FakeService:
    def __init__(self):
        self.h = None
        self.events = []
        self.msgs = []
        self.logged = {}
        self.n = 0
        self.online = True

    def verify_user(self, username, password):
        return 1 if (username, password) == ("me", "pw") else None

    def current_handout(self, uid):
        return self.h

    def next_problem(self, uid):
        self.n += 1
        self.h = SimpleNamespace(
            handout_id=self.n, number=1, title="Two Sum",
            leetcode_url="https://leetcode.com/problems/two-sum/", paraphrase=None,
            started_at=datetime.now(), target_min=15,
            instruction="Give your approach in words.")
        return self.h

    def log_attempt(self, uid, handout_id, **kw):
        self.logged[handout_id] = kw
        self.h = None
        return {"outcome": "clean", "retry_action": "none"}

    def reveal(self, handout_id):
        if handout_id not in self.logged:
            raise RuntimeError("not logged")
        return {"number": 1, "title": "Two Sum", "pattern": "prefix-hashmap", "slot": "drill",
                "outcome": "clean", "previous_attempts": []}

    def burn_pattern(self, uid, slug, reason): pass

    def coach_online(self):
        return {"online": self.online, "last_seen": None, "reason": "fake"}

    def push_event(self, uid, kind, payload):
        self.events.append((kind, payload))

    def post_message(self, uid, role, content):
        self.msgs.append({"id": len(self.msgs) + 1, "role": role, "content": content})

    def unlock_editor(self, uid, handout_id):
        self.editor = True
        self.post_message(uid, "coach", "Editor open.")
        return {"ok": True}

    def lock_editor(self, uid, handout_id):
        self.editor = False
        return {"ok": True}

    def editor_open(self, uid, handout_id):
        return getattr(self, "editor", False)

    def messages(self, uid, since_id=0):
        return [m for m in self.msgs if m["id"] > since_id]

    def status(self, uid):
        return {"backlog": {"backlog": 3, "target_today": 5, "done_today": 1, "remaining": 4},
                "streak": {"current": 2, "longest": 5, "last14": [1, 0] * 7},
                "tier1": [{"pattern": "two-pointers", "clean": 1, "need": 3, "tag": ""}],
                "tier2_open": [], "retry_due": [], "refire_due": [], "blocklist": [],
                "weakest": [], "strengths": [], "clean_rate": 0.5}

    def report(self, uid, target_date):
        return {"throughput": {"completed": 3}}

    def history(self, uid):
        return [{"id": 1, "day": "2026-10-20", "number": 1, "title": "Two Sum",
                 "pattern": "prefix-hashmap", "pattern_name": "Prefix Hashmap", "target_min": 15,
                 "actual_min": 12, "bugs": 0, "hints": 0, "optimal": True, "derived_cold": True,
                 "outcome": "clean", "notes": "", "source": "app"},
                {"id": 2, "day": "2026-10-19", "number": 3, "title": "Longest Substring",
                 "pattern": "sliding-window", "pattern_name": "Sliding Window", "target_min": 15,
                 "actual_min": 30, "bugs": 2, "hints": 1, "optimal": True, "derived_cold": False,
                 "outcome": "solved", "notes": "", "source": "app"}]

    def lint(self, src):
        return [(1, "COST", "x")] if "bad" in src else []
