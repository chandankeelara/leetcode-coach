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
        """FALLBACK picker (the page's "Use the default pick"). The coach picks with hand_out."""
        return _s.next_problem(user_id)

    def request_next(self, user_id):
        return _s.request_next(user_id)

    def get_plan(self, user_id):
        return _s.get_plan(user_id)

    def set_plan(self, user_id, **fields):
        return _s.set_plan(user_id, **fields)

    def pattern_choices(self):
        return _s.pattern_choices()

    def log_attempt(self, user_id, handout_id, **kw):
        r = _s.log_attempt(user_id, handout_id=handout_id, **kw)
        r["retry_action"] = r.get("retry_action", r.get("retry", "none"))
        return r

    def reveal(self, handout_id):
        r = dict(_s.reveal(handout_id))
        if "previous_attempts" not in r:
            r["previous_attempts"] = self._previous(r.get("number"), handout_id)
        if "attempt" not in r:
            r["attempt"] = self._attempt(handout_id)
        a = r.get("attempt") or {}
        r.setdefault("outcome", a.get("outcome"))
        if "retry_state" not in r:
            r["retry_state"] = self._retry_state(r.get("number"), a, handout_id)
        return r

    @staticmethod
    def _attempt(handout_id):
        try:
            from sqlalchemy import select
            from ..db import session_scope
            from ..models import Attempt
            with session_scope() as s:
                a = s.scalars(select(Attempt).where(Attempt.handout_id == handout_id)
                              .order_by(Attempt.id.desc())).first()
                if a is None:
                    return None
                return {"day": str(a.day), "target_min": a.target_min, "actual_min": a.actual_min,
                        "bugs": a.bugs, "hints": a.hints, "optimal": a.optimal,
                        "derived_cold": a.derived_cold, "outcome": a.outcome, "notes": a.notes}
        except Exception:
            return None

    @staticmethod
    def _retry_state(number, attempt, attempt_handout_id=None):
        try:
            from sqlalchemy import select
            from ..db import session_scope
            from ..models import RetryItem
            with session_scope() as s:
                uid = _handout_user(s, attempt_handout_id) if attempt_handout_id else None
                q = select(RetryItem).where(RetryItem.problem_number == number)
                if uid is not None:
                    q = q.where(RetryItem.user_id == uid)
                it = s.scalars(q.order_by(RetryItem.id.desc())).first()
                if it is None:
                    return {"state": "none"}
                if it.cleared_on is None:
                    return {"state": "queued", "attempts": it.attempts, "reason": it.reason}
                if attempt and str(it.cleared_on) == attempt.get("day"):
                    return {"state": "cleared"}
                return {"state": "none"}
        except Exception:
            return None

    def history(self, user_id):
        from sqlalchemy import select
        from ..catalog import pattern_name
        from ..db import session_scope
        from ..models import Attempt, Problem
        with session_scope() as s:
            rows = s.execute(select(Attempt, Problem.title, Problem.pattern_slug)
                             .join(Problem, Problem.number == Attempt.problem_number)
                             .where(Attempt.user_id == user_id)
                             .order_by(Attempt.day.desc(), Attempt.id.desc())).all()
            out = []
            for a, title, p_slug in rows:
                slug = a.pattern_slug or p_slug
                out.append({"id": a.id, "day": str(a.day), "number": a.problem_number,
                            "title": title, "pattern": slug,
                            "pattern_name": pattern_name(slug) if slug else None,
                            "target_min": a.target_min, "actual_min": a.actual_min,
                            "bugs": a.bugs, "hints": a.hints, "optimal": a.optimal,
                            "derived_cold": a.derived_cold, "outcome": a.outcome,
                            "notes": a.notes, "source": a.source})
            return out

    @staticmethod
    def _previous(number, handout_id):
        try:
            from sqlalchemy import select
            from ..db import session_scope
            from ..models import Attempt
            with session_scope() as s:
                # Only this user's history: the handout tells us whose it is.
                uid = _handout_user(s, handout_id)
                rows = s.scalars(select(Attempt).where(Attempt.problem_number == number,
                                                       Attempt.user_id == uid)
                                 .order_by(Attempt.day, Attempt.id)).all()
                return [{"day": str(a.day), "actual_min": a.actual_min, "bugs": a.bugs,
                         "outcome": a.outcome} for a in rows if a.handout_id != handout_id]
        except Exception:
            return []

    def burn_pattern(self, user_id, slug, reason):
        return _s.burn_pattern(user_id, slug, reason)

    def coach_online(self, user_id):
        return _s.coach_online(user_id)

    def push_event(self, user_id, kind, payload):
        return _s.push_event(user_id, kind, payload)

    def post_message(self, user_id, role, content):
        return _s.post_message(user_id, role, content)

    def editor_open(self, user_id, handout_id):
        return _s.editor_open(user_id, handout_id)

    def clock_state(self, user_id, handout_id):
        return _s.clock_state(user_id, handout_id)

    def messages(self, user_id, since_id=0):
        f = getattr(_s, "messages", None) or _s.list_messages
        return f(user_id, since_id)

    def thread_messages(self, user_id, handout_id, since_id=0):
        return _s.thread_messages(user_id, handout_id, since_id)

    def ui_events_since(self, user_id, since_id=0):
        return _s.ui_events_since(user_id, since_id)

    def ui_last_id(self):
        return _s.ui_last_id()

    def last_message_id(self, user_id):
        return _s.last_message_id(user_id)

    def visuals(self, user_id, handout_id, since_id=0):
        return _s.visuals(user_id, handout_id, since_id)

    def count_visuals(self, user_id, handout_id):
        return _s.count_visuals(user_id, handout_id)

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

    # ---- steps, rating, feedback (coach/steps.py, coach/rating.py, coach/feedback.py) ----
    def understood(self, user_id, handout_id):
        return _s.understood(user_id, handout_id)

    def skip_review(self, user_id, handout_id):
        return _s.skip_review(user_id, handout_id)

    def rating_history(self, user_id, n=20):
        return _s.rating_history(user_id, n)

    def is_admin(self, user_id):
        return _s.is_admin(user_id)

    def list_requests(self, **kw):
        return _s.list_requests(**kw)

    def set_request_status(self, request_id, status):
        return _s.set_request_status(request_id, status)


def _handout_user(s, handout_id):
    """Whose handout is this? Used to scope reveal lookups to one user."""
    from ..models import Handout
    h = s.get(Handout, handout_id)
    return h.user_id if h else None


# ---- editor tools (coach/editor.py via coach.service) -----------------------------------------
def _delegate(name):
    def method(self, *a, **kw):
        return getattr(_s, name)(*a, **kw)
    method.__name__ = name
    return method


for _n in ("editor_state", "save_draft", "read_code", "set_stub", "mark_op_applied",
           "resolve_annotation", "reply_annotation", "annotations", "scratches"):
    setattr(Svc, _n, _delegate(_n))


# ---- signup branch: who owns a handout (reveal pages are per user) ----------------------------
def _handout_owner(self, handout_id):
    from ..db import session_scope
    with session_scope() as s:
        return _handout_user(s, handout_id)


Svc.handout_owner = _handout_owner
