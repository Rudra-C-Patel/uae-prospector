import datetime as dt
import json
import os
import stat
import zipfile

import pytest

import prospector as pr

GOOD_NOTE = "Hi Sam, saw your service expansion. I'm a student founder helping dealers stop losing money they already earned. Would love to connect."
GOOD_DM = ("Thanks for connecting, Sam. I help dealers stop losing money they already earned. A 30-day free pilot "
           "uses your DMS/CRM export. Could we do a 20-minute virtual coffee between 8 and 11 AM UAE time? Rudra")


# ---------------------------------------------------------------- LinkedIn is never fetched
@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/in/someone", "https://linkedin.com/company/x", "https://ae.linkedin.com/in/x",
    "https://lnkd.in/abc123", "https://media.licdn.com/img.png", "HTTPS://WWW.LINKEDIN.COM/in/x",
])
def test_linkedin_hosts_blocked(url):
    assert pr.is_blocked(url)
    with pytest.raises(ValueError):
        pr.fetch_text(url)


@pytest.mark.parametrize("url", [
    "https://example.com/linkedin.com-tips", "https://notlinkedin.com/x", "https://www.alfuttaim.com/about",
])
def test_non_linkedin_hosts_allowed(url):
    assert not pr.is_blocked(url)


def test_redirect_to_linkedin_refused():
    h = pr._NoLinkedInRedirects()
    with pytest.raises(ValueError):
        h.redirect_request(None, None, 302, "Found", {}, "https://www.linkedin.com/authwall")


def test_linkedin_search_is_only_a_url():
    url = pr.linkedin_search("Sam Lee", "Al Futtaim")
    assert url.startswith("https://www.linkedin.com/search/results/people/?keywords=")
    assert "Sam%20Lee%20Al%20Futtaim" in url


# ---------------------------------------------------------------- categorize / score
@pytest.mark.parametrize("company,title,cat", [
    ("Al-Futtaim Motors", "Sales Exec", "automotive"),
    ("Gulf Toyota", "Service Manager", "automotive"),
    ("Shorooq Partners", "Partner", "investor"),
    ("Acme Logistics", "Engineer", "tech/business"),
])
def test_categorize(company, title, cat):
    assert pr.categorize(company, title) == cat


def test_score_buyer_titles_and_uae_dealer_bonus():
    # automotive base 45 + GM 35 + UAE 10 + known UAE dealer group 5
    assert pr.score("automotive", "General Manager", "Al-Futtaim Motors") == 95
    assert pr.score("automotive", "General Manager", "Some Motors") == 90
    assert pr.score("automotive", "General Manager", "Some Motors", in_uae=False) == 80


def test_partner_only_counts_big_for_investors():
    assert pr.score("investor", "Partner") == 30 + 25 + 10
    assert pr.score("automotive", "Partner", "Some Motors") == 45 + 12 + 10


def test_highest_single_title_boost_wins():
    assert pr.score("tech/business", "CEO and Manager") == 10 + 30 + 10


def test_stale_source_penalty():
    old = str(dt.date.today().year - 3)
    assert pr.score("automotive", "CEO", "x motors", True, old + "-01-01") == pr.score("automotive", "CEO", "x motors") - 10


def test_score_is_clamped():
    assert 0 <= pr.score("tech/business", "", in_uae=False, source_date="1990") <= 100


# ---------------------------------------------------------------- draft validator
def test_good_draft_passes():
    assert pr.problems(GOOD_NOTE, GOOD_DM) == []


@pytest.mark.parametrize("note,dm,needle", [
    ("x" * 281 + " already earned", GOOD_DM, "chars"),
    (GOOD_NOTE + " Guaranteed results!", GOOD_DM, "banned"),
    (GOOD_NOTE, GOOD_DM + " We recover 30% more.", "banned"),
    (GOOD_NOTE, GOOD_DM.replace("20-minute", "quick"), "20-minute"),
    (GOOD_NOTE, GOOD_DM.replace("UAE ", ""), "8-11 AM UAE"),
    (GOOD_NOTE, GOOD_DM.replace("30-day free pilot", "trial"), "pilot"),
    (GOOD_NOTE.replace("already earned", "earned"), GOOD_DM.replace("already earned", "earned"), "already earned"),
    (GOOD_NOTE, GOOD_DM + " I've been following your work.", "banned"),
    ("Helping dealers stop losing money you already earned", GOOD_DM, "grammar"),
])
def test_bad_drafts_flagged(note, dm, needle):
    assert any(needle in e for e in pr.problems(note, dm))


# ---------------------------------------------------------------- tracker
def _person(name="Sam Lee", company="Gulf Motors", **kw):
    return {"name": name, "title": "GM", "company": company, "score": "50", **kw}


def test_upsert_dedupes_by_name_and_company():
    rows = []
    assert pr.upsert(rows, [_person()]) == 1
    assert pr.upsert(rows, [_person(title="Dealer Principal")]) == 0
    assert len(rows) == 1 and rows[0]["title"] == "Dealer Principal"
    assert rows[0]["status"] == "to-send"
    assert pr.upsert(rows, [_person(company="Other Motors")]) == 1


def test_find_prefers_exact_match():
    rows = [{"name": "Sam Lee"}, {"name": "Sam Leeson"}]
    assert pr.find(rows, "sam lee") == [rows[0]]
    assert len(pr.find(rows, "sam")) == 2


def test_tracker_roundtrip_sorted_and_private(P):
    P.save_tracker([_person("A", score="10"), _person("B", score="90")])
    rows = P.load_tracker()
    assert [r["name"] for r in rows] == ["B", "A"]
    assert stat.S_IMODE(os.stat(P.TRACKER).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(P.DATA).st_mode) == 0o700


def test_read_connections_skips_linkedin_preamble(tmp_path):
    csv_text = "Notes:\n\"preamble line\"\n\nFirst Name,Last Name,URL,Email Address,Company,Position,Connected On\n" \
               "Sam,Lee,https://www.linkedin.com/in/sam,,Al-Futtaim Motors,General Manager,01 Jan 2026\n"
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Connections.csv", csv_text)
    rows = pr.read_connections(str(z))
    assert rows[0]["First Name"] == "Sam" and rows[0]["Company"] == "Al-Futtaim Motors"


def test_parse_keeps_only_uae_people(P, tmp_path, capsys):
    csv_text = ("First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
                "Sam,Lee,https://www.linkedin.com/in/sam,,Al-Futtaim Motors,General Manager,01 Jan 2026\n"
                "Ana,Ruiz,https://www.linkedin.com/in/ana,,Acme Corp Texas,CEO,01 Jan 2026\n")
    f = tmp_path / "Connections.csv"
    f.write_text(csv_text)
    assert P.cmd_parse([str(f)]) == 0
    rows = P.load_tracker()
    assert [r["name"] for r in rows] == ["Sam Lee"]
    assert rows[0]["status"] == "accepted"


def test_research_import_requires_source_url(P, tmp_path, capsys):
    f = tmp_path / "r.json"
    f.write_text(json.dumps([
        {"name": "Has Source", "title": "CEO", "company": "Gulf Motors", "source_url": "https://example.com/a"},
        {"name": "No Source", "title": "CEO", "company": "Gulf Motors"},
    ]))
    P.cmd_research([str(f)])
    assert [r["name"] for r in P.load_tracker()] == ["Has Source"]
    assert "No Source" in capsys.readouterr().out


def test_add_requires_public_url(P):
    assert P.cmd_add(["N", "CEO", "Co", "Dubai", "not-a-url", "why"]) == 1
    assert P.load_tracker() == []
    assert P.cmd_add(["N", "CEO", "Co", "Dubai", "https://example.com", "why"]) == 0


# ---------------------------------------------------------------- daily cap
def test_today_respects_daily_cap(P, capsys):
    today = P.today()
    rows = [_person(f"Sent {i}", status="sent", sent_date=today) for i in range(P.DAILY_CAP)]
    rows += [_person("Waiting", status="to-send")]
    P.save_tracker(rows)
    P.cmd_today([])
    out = capsys.readouterr().out
    assert "daily cap" in out and "Waiting" not in out


def test_today_shows_at_most_remaining_room(P, capsys):
    today = P.today()
    rows = [_person(f"Sent {i}", status="sent", sent_date=today) for i in range(P.DAILY_CAP - 2)]
    rows += [_person(f"Wait {i}", status="to-send") for i in range(5)]
    P.save_tracker(rows)
    P.cmd_today([])
    assert "Today's batch: 2" in capsys.readouterr().out


def test_mark_two_word_status_and_dates(P):
    P.save_tracker([_person("Sam Lee", status="sent")])
    assert P.cmd_mark(["Sam", "Lee", "chat", "booked", "--note", "Tue 9am"]) == 0
    r = P.load_tracker()[0]
    assert r["status"] == "chat booked" and r["chat_date"] == P.today() and "Tue 9am" in r["notes"]


def test_mark_rejects_unknown_status(P):
    P.save_tracker([_person("Sam Lee")])
    assert P.cmd_mark(["Sam Lee", "emailed"]) == 1


# ---------------------------------------------------------------- source verification
def _page(monkeypatch, text):
    monkeypatch.setattr(pr, "fetch_text", lambda url, timeout=20: text)


def test_verify_yes_when_title_beside_full_name(monkeypatch):
    _page(monkeypatch, "Press release: Sam Lee, General Manager of Gulf Motors, said today ...")
    assert pr.check_person({"name": "Sam Lee", "title": "General Manager", "source_url": "u"})[0] == "yes"


def test_verify_likely_when_title_words_nearby(monkeypatch):
    _page(monkeypatch, "Our team. Sam Lee joined in 2019 and today leads the group as its general " + "x " * 10 + "manager.")
    assert pr.check_person({"name": "Sam Lee", "title": "General Manager", "source_url": "u"})[0] == "likely"


def test_verify_no_when_name_missing(monkeypatch):
    _page(monkeypatch, "Nothing about this person here.")
    assert pr.check_person({"name": "Sam Lee", "title": "GM", "source_url": "u"})[0] == "no"


def test_verify_unreachable_on_fetch_error(monkeypatch):
    def boom(url, timeout=20):
        raise OSError("down")
    monkeypatch.setattr(pr, "fetch_text", boom)
    assert pr.check_person({"name": "Sam Lee", "title": "GM", "source_url": "u"})[0] == "unreachable"


def test_draft_one_never_calls_network_when_cached(P):
    os.makedirs(P.DRAFTS)
    with open(os.path.join(P.DRAFTS, "sam-lee.json"), "w") as f:
        json.dump({"name": "Sam Lee", "note": "n", "dm": "d", "issues": [], "note_chars": 1}, f)
    assert P.draft_one({"name": "Sam Lee"})["note"] == "n"
