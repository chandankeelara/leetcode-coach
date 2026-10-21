"""Adapter between the web app and coach.service. The web layer only ever calls these methods,
so tests can swap in a fake with the same names. Handles small differences between the written
contract and the service as built (names, shapes)."""
from datetime import date

from .. import service as _s


class Svc:
    def verify_user(self, username, password):
        return _s.verify_user(username, password)

    def current_handout(self, user_id):
        if hasattr(_s, "current_handout"):
            return _s.current_handout(user_id)
        from sqlalchemy import select
        from .. import picker
        from ..db import session_scope
        from ..models import Handout, Problem
        with session_scope() as s:
            h = s.scalars(select(Handout).where(
                Handout.user_id == user_id, Handout.status == "open",
                Handout.slot != "imported").order_by(Handout.id.desc())).first()
            if h is None or h.handed_at.date() != date.today():
                return None
            return picker.handoff_for(h, s.get(Problem, h.problem_number))

    def next_problem(self, user_id):
        return _s.next_problem(user_id)

    def log_attempt(self, user_id, handout_id, **kw):
        r = _s.log_attempt(user_id, handout_id=handout_id, **kw)
        r["retry_action"] = r.get("retry_action", r.get("retry", "none"))
        return r

    def reveal(self, handout_id):
        r = dict(_s.reveal(handout_id))
        if "previous_attempts" not in r:
            r["previous_attempts"] = self._previous(r.get("number"), handout_id)
        return r

    @staticmethod
    def _previous(number, handout_id):
        try:
            from sqlalchemy import select
            from ..db import session_scope
            from ..models import Attempt
            with session_scope() as s:
                rows = s.scalars(select(Attempt).where(Attempt.problem_number == number)
                                 .order_by(Attempt.day, Attempt.id)).all()
                return [{"day": str(a.day), "actual_min": a.actual_min, "bugs": a.bugs,
                         "outcome": a.outcome} for a in rows if a.handout_id != handout_id]
        except Exception:
            return []

    def burn_pattern(self, user_id, slug, reason):
        return _s.burn_pattern(user_id, slug, reason)

    def push_event(self, user_id, kind, payload):
        return _s.push_event(user_id, kind, payload)

    def post_message(self, user_id, role, content):
        return _s.post_message(user_id, role, content)

    def messages(self, user_id, since_id=0):
        f = getattr(_s, "messages", None) or _s.list_messages
        return f(user_id, since_id)

    def status(self, user_id):
        st = dict(_s.status(user_id))
        tiers = st.get("tiers")
        if isinstance(tiers, dict):
            st.setdefault("tier1", tiers.get("tier1", []))
            st.setdefault("tier2_open", tiers.get("tier2_open", []))
            st.setdefault("clean_rate", tiers.get("clean_rate"))
        st.setdefault("retry_due", st.get("retry", []))
        st.setdefault("refire_due", st.get("refire", []))
        st.setdefault("strengths", st.get("strongest", []))
        return st

    def report(self, user_id, target_date):
        return _s.report(user_id, target_date)

    def lint(self, src):
        from ..lint import lint
        return lint(src)
