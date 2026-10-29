"""Hint cards: the coach reads his code privately, works out the stage he is at, and offers
ONE hidden hint. The thread shows "Need a hint?" + the stage; the hint text is not in the page
until he presses "Show hint" (POST /hint/{id}/reveal). Opening it counts as one hint.

    offer_hint(user_id, handout_id, hint, stage, level=1)
        -> {ok, id, message_id, level, stage, hints_revealed, replaced}
        Leak-guarded like say; at most one unrevealed hint card: a new offer replaces it.
    reveal_hint(user_id, card_id)  -> {ok, html, hint, stage, level, hints_revealed}
        marks revealed, pushes `hint_revealed` {id, handout_id, stage, level, hints_revealed}
    dismiss_hint(user_id, card_id) -> {ok, html}     pushes `hint_dismissed` {id, handout_id,
                                      stage, level}; not counted
    hints_revealed(s, handout_id)  -> int            revealed cards of one handout

Stored on the card: Card.data = {stage, level, hint, revealed_at, dismissed_at}.
"""
import re
from datetime import datetime

from markupsafe import Markup
from sqlalchemy import delete, select

from .db import session_scope
from .models import Card, CoachMessage, Handout, Problem

HINT_MAX = 400
STAGE_MAX = 40
LEVELS = (1, 2, 3)
CODE_LINES_MAX = 3


def _svc():
    from . import service
    return service


def _open_handout(s, user_id, handout_id):
    h = s.get(Handout, handout_id) if handout_id is not None else None
    if h is None or h.user_id != user_id:
        raise ValueError(f"no handout {handout_id}")
    if h.status != "open":
        raise ValueError("that problem is not open")
    return h


def _hint_cards(s, handout_id):
    return list(s.scalars(select(Card).where(
        Card.handout_id == handout_id, Card.kind == "hint").order_by(Card.id)))


def hints_revealed(s, handout_id):
    return sum(1 for c in _hint_cards(s, handout_id) if (c.data or {}).get("revealed_at"))


def _unrevealed(c):
    d = c.data or {}
    return not d.get("revealed_at") and not d.get("dismissed_at")


def _check_code_blocks(text):
    for block in re.findall(r"```(.*?)```", text, re.S):
        lines = [ln for ln in block.strip("\n").split("\n") if ln.strip()]
        if len(lines) > CODE_LINES_MAX:
            raise ValueError(f"hint: a code block is at most {CODE_LINES_MAX} lines")


def offer_hint(user_id, handout_id, hint, stage, level=1):
    hint = (hint or "").strip()
    stage = (stage or "").strip()
    if not hint:
        raise ValueError("hint: empty")
    if len(hint) > HINT_MAX:
        raise ValueError(f"hint: {HINT_MAX} characters max")
    if not stage:
        raise ValueError("stage: a short label of where his code is")
    if len(stage) > STAGE_MAX:
        raise ValueError(f"stage: {STAGE_MAX} characters max")
    if level not in LEVELS:
        raise ValueError("level: 1 (nudge), 2 (targeted trace question) or 3 (name the part)")
    _check_code_blocks(hint)
    svc = _svc()
    replaced = None
    with session_scope() as s:
        h = _open_handout(s, user_id, handout_id)
        title = s.get(Problem, h.problem_number).title
        text = hint + "\n" + stage
        bad = svc.leak_words(text, title, s, h)
        if bad:
            raise ValueError("leak: " + ", ".join(bad))
        svc.rule_zero_check(s, user_id, text, h)
        for old in _hint_cards(s, h.id):
            if _unrevealed(old):
                replaced = old.id
                s.execute(delete(CoachMessage).where(CoachMessage.card_id == old.id))
                s.delete(old)
        s.flush()
        c = Card(user_id=user_id, handout_id=h.id, kind="hint", created_at=datetime.now(),
                 data={"stage": stage, "level": level, "hint": hint,
                       "revealed_at": None, "dismissed_at": None})
        s.add(c)
        s.flush()
        m = CoachMessage(user_id=user_id, handout_id=h.id, role="coach", content="Need a hint?",
                         card_id=c.id)
        s.add(m)
        s.flush()
        svc.emit(user_id, "message", m.id, s)
        out = {"ok": True, "id": c.id, "message_id": m.id, "level": level, "stage": stage,
               "hints_revealed": hints_revealed(s, h.id), "replaced": replaced}
    return out


def _mine(s, user_id, card_id):
    c = s.get(Card, card_id)
    if c is None or c.user_id != user_id or c.kind != "hint":
        raise ValueError(f"no hint {card_id}")
    return c


def reveal_hint(user_id, card_id):
    with session_scope() as s:
        c = _mine(s, user_id, card_id)
        d = dict(c.data or {})
        if d.get("dismissed_at"):
            raise ValueError("that hint was dismissed")
        first = not d.get("revealed_at")
        if first:
            d["revealed_at"] = datetime.now().isoformat()
            c.data = d
            s.flush()
        n = hints_revealed(s, c.handout_id)
        html = str(card_html(c))
        ev = {"id": c.id, "handout_id": c.handout_id, "stage": d["stage"], "level": d["level"],
              "hints_revealed": n}
        hint = d["hint"]
    if first:
        _svc().push_event(user_id, "hint_revealed", ev)
    return {"ok": True, "html": html, "hint": hint, "stage": ev["stage"], "level": ev["level"],
            "hints_revealed": n}


def dismiss_hint(user_id, card_id):
    with session_scope() as s:
        c = _mine(s, user_id, card_id)
        d = dict(c.data or {})
        if d.get("revealed_at"):
            raise ValueError("already opened")
        first = not d.get("dismissed_at")
        if first:
            d["dismissed_at"] = datetime.now().isoformat()
            c.data = d
            s.flush()
        html = str(card_html(c))
        ev = {"id": c.id, "handout_id": c.handout_id, "stage": d["stage"], "level": d["level"]}
    if first:
        _svc().push_event(user_id, "hint_dismissed", ev)
    return {"ok": True, "html": html}


def card_html(c):
    d = c.data or {}
    stage = d.get("stage") or ""
    if d.get("dismissed_at"):
        return Markup('<div class="rq rq-hint" data-card="{}" data-state="dismissed">'
                      '<p class="rq-cap">Hint dismissed.</p></div>').format(c.id)
    if d.get("revealed_at"):
        return Markup('<div class="rq rq-hint hint-card" data-card="{}" data-state="revealed">'
                      '<p class="rq-q">Hint <span class="hint-stage">{}</span></p>'
                      '<p class="hint-text">{}</p></div>').format(c.id, stage, d.get("hint") or "")
    return Markup('<div class="rq rq-hint hint-card" data-card="{id}" data-state="open">'
                  '<p class="rq-q">Need a hint?</p><p class="hint-stage">{stage}</p>'
                  '<div class="hint-actions"><button class="btn btn-primary btn-sm hint-show" '
                  'type="button" data-card="{id}">Show hint</button>'
                  '<button class="hint-no" type="button" data-card="{id}">Not now</button></div>'
                  '<p class="rq-msg" role="status" aria-live="polite"></p></div>') \
        .format(id=c.id, stage=stage)
