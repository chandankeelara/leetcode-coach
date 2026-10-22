"""Signup, invites, the connect step, the live rating card on reveal, instant abandoned score."""
import re

import pytest
from fastapi.testclient import TestClient

from coach import db, service, signup
from coach.models import Invite, RatingEvent, User
from coach.web import app as webapp
from coach.web.svc import Svc
from tests.test_mmr import give, parts
from tests.test_web import assert_clean

PW = "Sup3r-secret-pw"
GOOD = dict(username="newkid", email="Kid@Example.com", password=PW, confirm=PW)


@pytest.fixture
def site(world, monkeypatch):
    """The real app on the real (temp) database, one user `tester`, signup mode invite."""
    monkeypatch.setenv("COACH_SECRET", "test-secret")
    monkeypatch.delenv("COACH_SIGNUP", raising=False)
    monkeypatch.delenv("COACH_TRUST_PROXY", raising=False)
    signup.reset_limits()
    webapp.app.state.service = Svc()
    c = TestClient(webapp.app, follow_redirects=False)
    yield c, world
    webapp.app.state.service = None
    signup.reset_limits()


def token(c, path="/signup"):
    return re.search(r'name="csrf" value="([^"]+)"', c.get(path).text).group(1)


def post_signup(c, invite="", **over):
    form = dict(GOOD, **over)
    form["invite"] = invite
    form["csrf"] = token(c)
    return c.post("/signup", data=form)


def login_as(c, name="tester", pw="pw-for-tests"):
    r = c.post("/login", data={"username": name, "password": pw, "csrf": token(c, "/login")})
    assert r.status_code == 303
    return c


def page_csrf(c, path="/connect"):
    return re.search(r'X-CSRF-Token": "([^"]+)"', c.get(path).text).group(1)


def make_admin(uid):
    with db.session_scope() as s:
        s.get(User, uid).is_admin = True


# ---- validation ----------------------------------------------------------------------------
@pytest.mark.parametrize("over,msg", [
    (dict(username="ab"), "Username"),
    (dict(username="a" * 25), "Username"),
    (dict(username="Bad Name"), "Username"),
    (dict(username="UPPER"), "Username"),
    (dict(email="nope"), "email address"),
    (dict(email="a@b"), "email address"),
    (dict(email="a b@c.com"), "email address"),
    (dict(password="Short1!", confirm="Short1!"), "at least 10"),
    (dict(password="abcdefghijklmn", confirm="abcdefghijklmn"), "mix at least two"),
    (dict(password="1234567890123", confirm="1234567890123"), "mix at least two"),
    (dict(username="samename12", password="samename12", confirm="samename12"), "same as the username"),
    (dict(password=PW, confirm=PW + "x"), "differ"),
])
def test_bad_input_refused(site, over, msg):
    c, _ = site
    code = signup.create_invite()
    r = post_signup(c, code, **over)
    assert r.status_code == 400 and msg in r.text
    with db.session_scope() as s:
        assert s.query(User).count() == 1                 # only tester
        assert s.query(Invite).one().used_by is None       # a failed signup keeps the code


def test_password_not_equal_to_username_mixed_case(site):
    c, _ = site
    r = post_signup(c, signup.create_invite(), username="newkid_2026", password="newkid_2026",
                    confirm="newkid_2026")
    assert r.status_code == 400 and "same as the username" in r.text


def test_username_and_email_unique(site):
    c, _ = site
    r = post_signup(c, signup.create_invite())
    assert r.status_code == 303
    c2 = TestClient(webapp.app, follow_redirects=False)
    r = post_signup(c2, signup.create_invite(), email="other@example.com")
    assert r.status_code == 400 and "username is taken" in r.text
    r = post_signup(c2, signup.create_invite(), username="another")
    assert r.status_code == 400 and "email already" in r.text
    with db.session_scope() as s:
        assert s.query(User).filter(User.email == "kid@example.com").count() == 1   # lowercase


def test_email_unique_index_in_db(site):
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        with db.session_scope() as s:
            s.add(User(username="a1", password_hash="x", email="same@x.io"))
            s.add(User(username="a2", password_hash="x", email="same@x.io"))
    with db.session_scope() as s:                           # NULL emails never collide
        s.add(User(username="b1", password_hash="x"))
        s.add(User(username="b2", password_hash="x"))


# ---- invites -------------------------------------------------------------------------------
def test_invite_required_and_used_once(site):
    c, _ = site
    r = post_signup(c, "")
    assert r.status_code == 400 and "invite code is needed" in r.text
    r = post_signup(c, "AAAA-BBBB-CCCC")
    assert r.status_code == 400 and "not valid" in r.text
    code = signup.create_invite(note="for sam")
    assert re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", code)
    r = post_signup(c, code.lower())                        # case and dashes forgiven
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    c2 = TestClient(webapp.app, follow_redirects=False)
    r = post_signup(c2, code, username="second", email="second@example.com")
    assert r.status_code == 400 and "already used" in r.text
    row = signup.list_invites()[0]
    assert row["status"] == "used" and row["used_by_name"] == "newkid" and row["note"] == "for sam"
    with db.session_scope() as s:
        assert s.query(User).filter_by(username="second").count() == 0


def test_revoked_invite_refused(site):
    c, _ = site
    code = signup.create_invite()
    assert signup.revoke_invite(code) is True
    r = post_signup(c, code)
    assert r.status_code == 400 and "not valid" in r.text
    assert signup.list_invites()[0]["status"] == "revoked"
    assert signup.revoke_invite(code) is False


def test_used_invite_cannot_be_revoked(site):
    c, _ = site
    code = signup.create_invite()
    assert post_signup(c, code).status_code == 303
    assert signup.revoke_invite(code) is False
    assert signup.list_invites()[0]["status"] == "used"


def test_invite_link_prefills_code(site):
    c, _ = site
    assert 'value="WXYZ-2345"' in c.get("/signup?invite=WXYZ-2345").text


# ---- modes ---------------------------------------------------------------------------------
def test_open_mode_needs_no_code(site, monkeypatch):
    c, _ = site
    monkeypatch.setenv("COACH_SIGNUP", "open")
    assert 'name="invite"' not in c.get("/signup").text
    assert post_signup(c).status_code == 303


def test_closed_mode(site, monkeypatch):
    c, _ = site
    monkeypatch.setenv("COACH_SIGNUP", "closed")
    page = c.get("/signup")
    assert page.status_code == 200 and "closed" in page.text
    assert 'name="username"' not in page.text
    r = c.post("/signup", data=dict(GOOD, invite="", csrf="x"))
    assert r.status_code == 403
    with db.session_scope() as s:
        assert s.query(User).count() == 1


@pytest.mark.parametrize("mode,shown", [(None, True), ("invite", True), ("open", True),
                                         ("closed", False), ("bogus", True)])
def test_login_link_visibility(site, monkeypatch, mode, shown):
    c, _ = site
    if mode:
        monkeypatch.setenv("COACH_SIGNUP", mode)
    assert ('href="/signup"' in c.get("/login").text) is shown


# ---- rate limit ----------------------------------------------------------------------------
def test_rate_limit_five_an_hour_per_ip(site):
    c, _ = site
    for i in range(5):
        assert post_signup(c, "BAD", username=f"user{i}x").status_code == 400
    r = post_signup(c, signup.create_invite())
    assert r.status_code == 429 and "Too many" in r.text
    assert signup.rate_limited("1.2.3.4", now=0) is False
    for _ in range(4):
        assert signup.rate_limited("1.2.3.4", now=1) is False
    assert signup.rate_limited("1.2.3.4", now=2) is True
    assert signup.rate_limited("5.6.7.8", now=2) is False           # per IP
    assert signup.rate_limited("1.2.3.4", now=3601) is False        # the window slides


def test_rate_limit_uses_forwarded_ip_only_when_trusted(site, monkeypatch):
    c, _ = site
    monkeypatch.setenv("COACH_TRUST_PROXY", "1")
    for i in range(5):
        r = c.post("/signup", data=dict(GOOD, invite="BAD", csrf=token(c)),
                   headers={"x-forwarded-for": "9.9.9.9, 10.0.0.1"})
        assert r.status_code == 400
    r = c.post("/signup", data=dict(GOOD, invite="BAD", csrf=token(c)),
               headers={"x-forwarded-for": "9.9.9.9"})
    assert r.status_code == 429
    r = c.post("/signup", data=dict(GOOD, invite="BAD", csrf=token(c)),
               headers={"x-forwarded-for": "8.8.8.8"})
    assert r.status_code == 400


# ---- csrf, redirect, connect step ----------------------------------------------------------
def test_csrf_required(site):
    c, _ = site
    code = signup.create_invite()
    r = c.post("/signup", data=dict(GOOD, invite=code, csrf="forged"))
    assert r.status_code == 303 and r.headers["location"] == "/signup"
    r = c.post("/signup", data=dict(GOOD, invite=code))
    assert r.headers["location"] == "/signup"
    login_tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/login").text).group(1)
    r = c.post("/signup", data=dict(GOOD, invite=code, csrf=login_tok))     # a login token
    assert r.headers["location"] == "/signup"
    with db.session_scope() as s:
        assert s.query(User).count() == 1


def test_signup_logs_in_and_redirects_to_onboarding_then_connect_step_once(site):
    c, _ = site
    r = post_signup(c, signup.create_invite())
    assert r.status_code == 303 and r.headers["location"] == "/onboarding"
    assert "coach_session" in r.cookies
    on = c.get("/onboarding")
    assert on.status_code == 200 and "target_date" in on.text
    t = re.search(r'X-CSRF-Token": "([^"]+)"', on.text).group(1)
    r = c.post("/onboarding", data={"csrf": t, "target_date": "2026-12-15",
                                    "daily_problems": "2", "minutes_per_day": "90"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    first = c.get("/")
    assert first.status_code == 200 and "Connect your coach" in first.text
    assert 'href="/connect"' in first.text
    assert_clean(first.text)                                 # RULE ZERO words unaffected
    second = c.get("/")
    assert "Connect your coach" not in second.text           # one time only
    with db.session_scope() as s:
        u = s.query(User).filter_by(username="newkid").one()
        assert u.email == "kid@example.com" and u.created_at is not None
        assert u.password_hash.startswith("$argon2")
    assert service.verify_user("newkid", PW) == u.id


def test_logged_in_visitor_skips_signup_page(site):
    c, _ = site
    login_as(c)
    assert c.get("/signup").headers["location"] == "/"


def test_existing_user_login_has_no_connect_step(site):
    c, _ = site
    login_as(c)
    assert "Connect your coach" not in c.get("/").text


# ---- admin invites page --------------------------------------------------------------------
def test_invites_page_admin_only(site):
    c, uid = site
    assert c.get("/admin/invites").headers["location"] == "/login"
    login_as(c)
    assert c.get("/admin/invites").status_code == 404
    t = page_csrf(c)
    assert c.post("/admin/invites", data={"csrf": t, "note": "x"}).status_code == 404
    assert c.post("/admin/invites/AAAA/revoke", data={"csrf": t}).status_code == 404
    make_admin(uid)
    assert c.get("/admin/invites").status_code == 200


def test_admin_creates_revokes_and_sees_usage(site):
    c, uid = site
    make_admin(uid)
    login_as(c)
    t = page_csrf(c)
    assert c.post("/admin/invites", data={"csrf": "bad"}).status_code == 403
    r = c.post("/admin/invites", data={"csrf": t, "note": "for ana"})
    code = re.search(r'id="new-invite">([A-Z2-9-]+)<', r.text).group(1)
    assert "for ana" in r.text and "unused" in r.text
    other = signup.create_invite(uid, "to burn")
    r = c.post(f"/admin/invites/{other}/revoke", data={"csrf": t})
    assert r.status_code == 303
    c2 = TestClient(webapp.app, follow_redirects=False)
    assert post_signup(c2, code).status_code == 303
    page = c.get("/admin/invites").text
    assert 'data-status="used"' in page and "newkid" in page
    assert 'data-status="revoked"' in page and 'data-status="unused"' not in page
    assert post_signup(TestClient(webapp.app, follow_redirects=False), other,
                       username="late", email="l@x.io").status_code == 400


def test_cli_invite(site, capsys):
    from coach import cli
    cli.main(["invite", "--note", "cli made"])
    code = capsys.readouterr().out.strip()
    rows = signup.list_invites()
    assert rows[0]["code"] == code and rows[0]["note"] == "cli made"
    assert rows[0]["created_by_name"] is None


# ---- rating: live event, pending card, abandoned instant ------------------------------------
def logged(uid, **kw):
    h = give(uid)
    service.log_attempt(uid, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                        derived_cold=True, **kw)
    return h.handout_id


def rating_events(uid, since=0):
    return [e for e in service.ui_events_since(uid, since) if e["kind"] == "rating"]


def test_score_attempt_emits_rating_event(site):
    _, uid = site
    hid = logged(uid)
    assert rating_events(uid) == []
    service.score_attempt(uid, hid, parts())
    ev = rating_events(uid)
    assert len(ev) == 1 and ev[0]["ref_id"] == hid


def test_refused_score_emits_nothing(site):
    _, uid = site
    hid = logged(uid)
    bad = parts()
    bad["quality"]["why"] = "tester did it"
    with pytest.raises(service.ScoreRefused):
        service.score_attempt(uid, hid, bad)
    assert rating_events(uid) == []


def test_stream_delivers_rating_event(site):
    c, uid = site
    login_as(c)
    hid = logged(uid)
    since = service.ui_last_id()
    service.score_attempt(uid, hid, parts())
    body = c.get(f"/stream?since={since}&max_events=2&max_s=5").text
    assert "event: rating" in body and f'"ref_id": {hid}' in body


def test_reveal_pending_card_then_scored(site):
    c, uid = site
    login_as(c)
    hid = logged(uid)
    page = c.get(f"/reveal/{hid}").text
    assert "Your coach is scoring this solve" in page
    assert 'id="rv-rating-slot"' in page and f'data-handout="{hid}"' in page
    assert 'data-pending="1"' in page and "/static/reveal.js" in page
    assert "eload" not in page.split('id="rv-rating-pending"')[1].split("</section>")[0]
    r = c.get(f"/reveal/{hid}/rating")
    d = r.json()
    assert d["pending"] is True and "scoring this solve" in d["html"]
    service.score_attempt(uid, hid, parts(), summary="all his own")
    d = c.get(f"/reveal/{hid}/rating").json()
    assert d["pending"] is False and "scoring this solve" not in d["html"]
    assert "scored by your coach" in d["html"] and "all his own" in d["html"]
    assert "understand reason" in d["html"] and "1200" in d["html"]
    assert 'data-pending="0"' in c.get(f"/reveal/{hid}").text


def test_rank_up_in_fragment(site):
    from datetime import datetime
    from coach.models import Rating
    c, uid = site
    login_as(c)
    with db.session_scope() as s:
        s.add(Rating(user_id=uid, mmr=1395.0, games=30, updated_at=datetime.now()))
    hid = logged(uid)
    service.score_attempt(uid, hid, parts())
    html = c.get(f"/reveal/{hid}/rating").json()["html"]
    assert "Rank up" in html and "Gold" in html


def test_rating_fragment_scoped_to_owner(site):
    c, uid = site
    hid = logged(uid)
    other = service.create_user("someone", "another-pw-123")
    c2 = login_as(TestClient(webapp.app, follow_redirects=False), "someone", "another-pw-123")
    assert other != uid
    assert c2.get(f"/reveal/{hid}/rating").status_code == 404
    assert c2.get(f"/reveal/{hid}").status_code == 404
    assert c2.get("/reveal/99999/rating").status_code == 404
    assert TestClient(webapp.app, follow_redirects=False).get(
        f"/reveal/{hid}/rating").headers["location"] == "/login"


def test_live_js_listens_for_rating():
    from pathlib import Path
    root = Path(webapp.__file__).parent / "static"
    assert '"rating"' in (root / "live.js").read_text(encoding="utf-8")
    js = (root / "reveal.js").read_text(encoding="utf-8")
    assert 'on("rating"' in js and "3000" in js and "120000" in js and "/rating" in js


def test_abandoned_is_instant_zero_with_no_pending(site):
    c, uid = site
    login_as(c)
    h = give(uid)
    before = service.get_rating(uid)["mmr"]
    res = service.log_attempt(uid, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                              derived_cold=True, abandoned=True)
    assert res["score_pending"] is False and res["rating"]["delta"] < 0
    rv = service.reveal(h.handout_id)
    assert not rv["score_pending"] and rv["rating"]["scored_by"] == "auto"
    assert rv["rating"]["actual"] == 0 and rv["rating"]["before"] == before
    assert rv["rating"]["after"] < before and rv["rating"]["breakdown"]["abandoned"]
    assert len(rating_events(uid)) == 1 and rating_events(uid)[0]["ref_id"] == h.handout_id
    page = c.get(f"/reveal/{h.handout_id}").text
    assert "scoring this solve" not in page and "scored 0 at once" in page
    assert 'data-pending="0"' in page


def test_abandoned_score_attempt_attaches_reasons_once_and_keeps_zero(site):
    _, uid = site
    h = give(uid)
    service.log_attempt(uid, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                        derived_cold=True, abandoned=True)
    first = service.reveal(h.handout_id)["rating"]
    mmr = service.get_rating(uid)["mmr"]
    out = service.score_attempt(uid, h.handout_id, parts(), summary="gave up at the approach")
    assert out["actual"] == 0 and out["delta"] == first["delta"] and out["after"] == first["after"]
    assert out["scored_by"] == "auto"
    rv = service.reveal(h.handout_id)["rating"]
    assert rv["actual"] == 0 and rv["delta"] == first["delta"] and rv["after"] == first["after"]
    assert rv["summary"] == "gave up at the approach" and rv["reasons_attached"]
    assert rv["breakdown"]["parts"]["approach"]["why"] == "approach reason"
    assert "score" not in rv["breakdown"]["parts"]["approach"]
    assert service.get_rating(uid)["mmr"] == mmr
    with db.session_scope() as s:
        assert s.query(RatingEvent).count() == 1
    with pytest.raises(service.ScoreRefused, match="already scored"):
        service.score_attempt(uid, h.handout_id, parts())
    assert len(rating_events(uid)) == 2                      # the reasons refresh the page


def test_abandoned_reasons_validated_and_nothing_changes(site):
    _, uid = site
    h = give(uid)
    service.log_attempt(uid, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                        derived_cold=True, abandoned=True)
    bad = parts()
    bad["time"]["why"] = "def f(a):\n    return a"
    with pytest.raises(service.ScoreRefused, match="no code"):
        service.score_attempt(uid, h.handout_id, bad)
    assert not service.reveal(h.handout_id)["rating"]["reasons_attached"]
    service.score_attempt(uid, h.handout_id, parts())        # still attachable


def test_abandoned_reasons_render_on_reveal(site):
    c, uid = site
    login_as(c)
    h = give(uid)
    service.log_attempt(uid, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                        derived_cold=True, abandoned=True)
    service.score_attempt(uid, h.handout_id, parts(), summary="stopped early")
    html = c.get(f"/reveal/{h.handout_id}/rating").json()["html"]
    assert "stopped early" in html and "approach reason" in html and "scored 0 at once" in html


def test_non_abandoned_log_still_pending(site):
    _, uid = site
    h = give(uid)
    res = service.log_attempt(uid, handout_id=h.handout_id, bugs=0, hints=0, optimal=True,
                              derived_cold=True)
    assert res["score_pending"] is True
    assert rating_events(uid) == []
