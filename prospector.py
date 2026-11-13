#!/usr/bin/env python3
"""UAE Prospector: local prospect list, drafts and tracker for DealerScope outreach.

Hard rules baked in:
  * Never touches LinkedIn beyond building a plain search URL for you to open.
  * Never sends anything. Drafts are printed / saved locally only.
  * LLM calls go only to the local Ollama at 127.0.0.1:11434.
  * All data lives in ./data (gitignored, chmod 700).
"""
import csv
import datetime as dt
import glob
import html
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
TRACKER = os.path.join(DATA, "tracker.csv")
RESEARCH = os.path.join(DATA, "research.json")
DRAFTS = os.path.join(DATA, "drafts")
BRIEFS = os.path.join(DATA, "briefs")

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = os.environ.get("PROSPECT_MODEL", "bonsai-2:27b")
DAILY_CAP = 15
TODAY_N = 10
NOTE_MAX = 280

STATUSES = ["to-send", "sent", "accepted", "chat booked", "pilot", "no"]
FIELDS = ["name", "title", "company", "city", "category", "score", "source", "source_url",
          "source_date", "verified", "why", "detail", "status", "added", "sent_date",
          "accepted_date", "chat_date", "notes", "linkedin_search"]

# ---------------------------------------------------------------- scoring

UAE_WORDS = ["uae", "dubai", "abu dhabi", "sharjah", "ajman", "ras al khaimah", "fujairah",
             "umm al quwain", "al ain", "emirates", "emirati", "difc", "adgm", "hub71", "masdar",
             "mubadala", "adnoc", "etisalat", "e&", "du ", "careem", "noon", "talabat", "dubizzle",
             "bayut", "property finder", "emaar", "dp world", "majid al futtaim"]
UAE_AUTO_COMPANIES = ["al-futtaim", "al futtaim", "al tayer", "agmc", "albatha", "al nabooda",
                      "gargash", "arabian automobiles", "awr", "rostamani", "al masaood",
                      "juma al majid", "al majid", "al habtoor", "galadari", "al ghandi",
                      "emirates motor", "al fahim", "ali & sons", "ali and sons", "liberty automobiles",
                      "trading enterprises", "premier motors", "elite motors", "yallamotor",
                      "dubicars", "carswitch", "kavak", "cars24", "autotrader uae", "al rostamani"]
AUTO_WORDS = ["motor", "automotive", "automobile", "auto ", "autos", "car ", "cars", "dealer",
              "dealership", "toyota", "nissan", "lexus", "bmw", "mercedes", "audi", "porsche",
              "hyundai", "kia", "ford", "honda", "mazda", "mitsubishi", "chevrolet", "gmc", "volvo",
              "jaguar", "land rover", "infiniti", "renault", "volkswagen", "geely", "mg motor",
              "byd", "chery", "dms", "cdk", "reynolds", "keyloop", "salesforce automotive",
              "auto finance", "car finance", "motor insurance", "leasing", "fleet", "mobility"]
INVESTOR_WORDS = ["ventures", "capital", "vc", "angel", "investor", "fund", "accelerator",
                  "incubator", "hub71", "in5", "dtec", "founder", "co-founder", "partners", "shorooq",
                  "beco", "global ventures", "mevp", "wamda", "stv", "500 global", "flat6labs"]

TITLE_BOOSTS = [
    (r"dealer principal|general manager\b|\bgm\b", 35),
    (r"after ?sales|service director|head of service|service manager|\bparts\b", 35),
    (r"\bcrm\b|digital|customer experience|\bcx\b|retention|loyalty", 35),
    (r"\bceo\b|chief executive|president|managing director|\bmd\b|founder|owner", 30),
    (r"\bpartner\b", 25),
    (r"director|head of|\bvp\b|vice president|chief|\bc[fotmi]o\b", 15),
    (r"manager|lead", 8),
]


def _has(text, words):
    t = f" {text.lower()} "
    return any(w in t for w in words)


def categorize(company, title):
    blob = f"{company} {title}"
    if _has(blob, UAE_AUTO_COMPANIES) or _has(blob, AUTO_WORDS):
        return "automotive"
    if _has(blob, INVESTOR_WORDS):
        return "investor"
    return "tech/business"


def score(category, title, company="", in_uae=True, source_date=""):
    """0-100. Automotive > investors/ecosystem > other; buyer titles boosted; stale sources dinged."""
    base = {"automotive": 45, "investor": 30, "tech/business": 10}[category]
    t = title.lower()
    boost = 0
    for pattern, pts in TITLE_BOOSTS:
        if re.search(pattern, t):
            boost = max(boost, pts)
    if category != "investor" and boost == 25:  # "Partner" only counts big for investors
        boost = 12
    bonus = 10 if in_uae else 0
    if category == "automotive" and _has(company, UAE_AUTO_COMPANIES):
        bonus += 5
    m = re.match(r"(\d{4})", source_date or "")
    if m and int(m.group(1)) < dt.date.today().year - 1:
        bonus -= 10  # title may be out of date
    return max(0, min(100, base + boost + bonus))


def linkedin_search(name, company):
    """Plain LinkedIn people-search URL. You open it yourself; this tool never requests it."""
    q = urllib.parse.quote(f"{name} {company}".strip())
    return f"https://www.linkedin.com/search/results/people/?keywords={q}"


# ---------------------------------------------------------------- storage

def ensure_dirs():
    for d in (DATA, DRAFTS, BRIEFS):
        os.makedirs(d, exist_ok=True)
    os.chmod(DATA, 0o700)


def load_tracker():
    if not os.path.exists(TRACKER):
        return []
    with open(TRACKER, newline="", encoding="utf-8") as f:
        return [dict({k: "" for k in FIELDS}, **r) for r in csv.DictReader(f)]


def save_tracker(rows):
    ensure_dirs()
    rows.sort(key=lambda r: -int(r.get("score") or 0))
    tmp = TRACKER + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, TRACKER)
    os.chmod(TRACKER, 0o600)


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def key(r):
    return (slug(r["name"]), slug(r.get("company", "")))


def find(rows, query):
    q = query.lower().strip()
    exact = [r for r in rows if r["name"].lower() == q]
    if exact:
        return exact
    return [r for r in rows if q in r["name"].lower()]


def today():
    return dt.date.today().isoformat()


def upsert(rows, new):
    idx = {key(r): r for r in rows}
    added = 0
    for n in new:
        k = key(n)
        if k in idx:
            cur = idx[k]
            for f in ("title", "city", "category", "score", "source", "source_url", "source_date",
                      "why", "detail", "linkedin_search"):
                if n.get(f):
                    cur[f] = n[f]
        else:
            n.setdefault("status", "to-send")
            n.setdefault("added", today())
            rows.append(dict({k2: "" for k2 in FIELDS}, **n))
            idx[k] = rows[-1]
            added += 1
    return added


# ---------------------------------------------------------------- 1. parse

def newest_export(path=None):
    if path:
        return path
    zips = sorted(glob.glob(os.path.expanduser("~/Downloads/*.zip")), key=os.path.getmtime, reverse=True)
    for z in zips:
        try:
            with zipfile.ZipFile(z) as zf:
                if any(n.lower().endswith("connections.csv") for n in zf.namelist()):
                    return z
        except zipfile.BadZipFile:
            continue
    loose = sorted(glob.glob(os.path.expanduser("~/Downloads/**/Connections.csv"), recursive=True),
                   key=os.path.getmtime, reverse=True)
    return loose[0] if loose else None


def read_connections(path):
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            name = next(n for n in zf.namelist() if n.lower().endswith("connections.csv"))
            text = zf.read(name).decode("utf-8-sig", errors="replace")
    else:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    # LinkedIn prepends a "Notes:" preamble before the real header row.
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith("First Name")), 0)
    return list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))


def cmd_parse(args):
    path = newest_export(args[0] if args else None)
    if not path:
        print("No LinkedIn export found. Request it at LinkedIn > Settings > Data privacy >\n"
              "'Get a copy of your data' > Connections, then put the zip in ~/Downloads and rerun.")
        return 1
    conns = read_connections(path)
    keep = []
    for c in conns:
        name = f"{c.get('First Name', '').strip()} {c.get('Last Name', '').strip()}".strip()
        company, title = c.get("Company", "").strip(), c.get("Position", "").strip()
        if not name:
            continue
        # The export has no location column, so UAE membership is inferred from the company.
        in_uae = _has(company, UAE_WORDS) or _has(company, UAE_AUTO_COMPANIES)
        if not in_uae:
            continue
        cat = categorize(company, title)
        keep.append({
            "name": name, "title": title, "company": company, "city": "UAE (inferred from company)",
            "category": cat, "score": str(score(cat, title, company, True)),
            "source": "linkedin-export", "source_url": c.get("URL", ""),
            "source_date": c.get("Connected On", ""), "verified": "own connection",
            "why": f"Existing 1st-degree connection ({cat}).", "detail": "",
            "status": "accepted", "linkedin_search": c.get("URL", "") or linkedin_search(name, company),
        })
    rows = load_tracker()
    n = upsert(rows, keep)
    save_tracker(rows)
    print(f"Read {len(conns)} connections from {os.path.basename(path)}; "
          f"{len(keep)} look UAE-based; {n} new rows added (status=accepted since you're connected).")
    print("Note: the export has no location field, so people at non-UAE-named companies are missed.\n"
          "Add them by hand with: prospect add.")
    return 0


# ---------------------------------------------------------------- 2. research import

def cmd_research(args):
    path = args[0] if args else RESEARCH
    with open(path, encoding="utf-8") as f:
        people = json.load(f)
    new, bad = [], []
    for p in people:
        if not p.get("source_url"):
            bad.append(p.get("name", "?"))
            continue
        cat = p.get("category") or categorize(p["company"], p["title"])
        new.append({
            "name": p["name"], "title": p["title"], "company": p["company"], "city": p.get("city", ""),
            "category": cat,
            "score": str(score(cat, p["title"], p["company"], True, p.get("source_date", ""))),
            "source": "public-web", "source_url": p["source_url"],
            "source_date": p.get("source_date", ""), "why": p.get("why", ""),
            "detail": p.get("detail", ""), "linkedin_search": linkedin_search(p["name"], p["company"]),
        })
    rows = load_tracker()
    n = upsert(rows, new)
    save_tracker(rows)
    print(f"Imported {len(new)} researched people ({n} new).")
    if bad:
        print("Skipped (no source URL, never added without one):", ", ".join(bad))
    return 0


# ---------------------------------------------------------------- verify

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Safari/605.1.15"


BLOCKED_HOSTS = ("linkedin.com", "lnkd.in", "licdn.com")


def is_blocked(url):
    """True for LinkedIn (and its short-link/CDN hosts), including subdomains."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in BLOCKED_HOSTS)


class _NoLinkedInRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if is_blocked(newurl):
            raise ValueError(f"refusing to follow redirect to LinkedIn: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_NoLinkedInRedirects)


def fetch_text(url, timeout=20):
    if is_blocked(url):
        raise ValueError("refusing to fetch LinkedIn")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with _OPENER.open(req, timeout=timeout) as r:
        raw = r.read(3_000_000).decode("utf-8", errors="replace")
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    txt = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", txt)


def _norm(s):
    return re.sub(r"[^a-z0-9 ]", " ", s.lower().replace("’", "'"))


STOP = {"and", "the", "of", "for", "uae", "at"}


def check_person(r, tight=12, loose=120):
    """Return (status, detail).
    yes     = full name on page and the exact title phrase within `tight` chars of it
              ("Name, Title" or "Title Name"); press releases and bios pass this.
    likely  = name on page and every title word within `loose` chars; on dense list pages
              this can be a neighbour's title, so confirm by hand.
    partial = name found, title not near it.   no = name not on page.  unreachable = fetch failed."""
    try:
        text = re.sub(r"\s+", " ", _norm(fetch_text(r["source_url"])))
    except Exception as e:  # noqa: BLE001
        return "unreachable", f"{type(e).__name__}: {str(e)[:80]}"
    parts = [p for p in _norm(r["name"]).split() if len(p) > 1]
    if not parts:
        return "no", "empty name"
    full = " ".join(parts)
    title_words = [w for w in _norm(r["title"]).split() if len(w) > 1 and w not in STOP]
    phrase = " ".join(title_words)
    spans = [(m.start(), m.end()) for m in re.finditer(re.escape(full), text)]
    kind = "full name"
    if not spans:
        spans = [(m.start(), m.end()) for m in re.finditer(r"\b" + re.escape(parts[-1]) + r"\b", text)]
        kind = "surname"
    if not spans:
        return "no", "name not found on page (JS-rendered? check by hand)"
    # Compare against the page with stop-words removed so "CEO of AGMC" == "ceo agmc".
    def squash(t):
        return " ".join(w for w in t.split() if w not in STOP)
    for s0, e0 in spans:
        after = squash(text[e0:e0 + tight + len(phrase) + 20])
        before = squash(text[max(0, s0 - len(phrase) - 20):s0])
        # "Name, Title" / "Name, the new Title" (small gap) or "Title Name" (no gap at all).
        if phrase and (phrase in after[:tight + len(phrase)] or before.endswith(phrase)):
            return ("yes" if kind == "full name" else "likely"), f"{kind} with title right beside it"
    for s0, e0 in spans:
        ctx = text[max(0, s0 - loose): e0 + loose]
        if all(re.search(r"\b" + re.escape(w) + r"\b", ctx) for w in title_words):
            return "likely", f"{kind} with title words nearby; confirm by hand"
    return "partial", f"{kind} found but '{r['title']}' not near it; check by hand"


def cmd_verify(args):
    rows = load_tracker()
    targets = [r for r in rows if r["source"] == "public-web"]
    if args and args[0] != "--all":
        targets = find(targets, " ".join(args))
    for r in targets:
        status, why = check_person(r)
        r["verified"] = f"{status} ({today()}): {why}"
        print(f"[{status:11}] {r['name']} | {r['title']} | {r['company']}\n              {why}")
    save_tracker(rows)
    return 0


# ---------------------------------------------------------------- 3. drafts (local LLM)

VOICE = """You write LinkedIn messages for Rudra, an 18-year-old founder at UC San Diego building DealerScope.
DealerScope finds revenue a dealership is losing (lost leads, missed service visits, lease-ends) from its
existing DMS/CRM export, and offers a 30-day free pilot.
Tone: respectful, warm, relationship-first, humble, plain English. No hype, no buzzwords, no exclamation
marks, no statistics or percentages, no claims about results, no "100%", no "guarantee". Never pretend to
know the person, to have met them, or to have "followed their work". Mention exactly one real detail and
only the one given below. Keep grammar consistent ("help dealers stop losing money they already earned",
or "help you stop losing money you already earned")."""

BANNED = [r"100\s*%", r"\d+\s*%", r"guarantee", r"revolution", r"game[- ]?chang", r"skyrocket",
          r"cutting[- ]edge", r"!", r"synerg", r"world[- ]class", r"\bproven\b", r"leverage",
          r"\bhuge\b", r"disrupt", r"\bmassive", r"10x", r"best[- ]in[- ]class",
          r"(i'?ve|i have|been) follow(ed|ing)", r"without risk", r"risk[- ]free", r"\badmire",
          r"resonated deeply", r"hidden revenue", r"revenue leaks?", r"you'?ve been doing amazing"]


def ollama(prompt, temperature=0.5):
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False, "format": "json",
                       "think": False, "options": {"temperature": temperature, "num_predict": 700}})
    req = urllib.request.Request(OLLAMA_URL, data=body.encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(json.loads(r.read())["response"])


def problems(note, dm):
    errs = []
    if len(note) > NOTE_MAX:
        errs.append(f"note is {len(note)} chars (max {NOTE_MAX})")
    for label, txt in (("note", note), ("dm", dm)):
        for b in BANNED:
            if re.search(b, txt, re.I):
                errs.append(f"{label} contains banned pattern {b}")
    if not re.search(r"already earned", note + " " + dm, re.I):
        errs.append("missing the 'stop losing money you already earned' idea")
    if not re.search(r"20[- ]?min", dm, re.I):
        errs.append("dm must ask for a 20-minute chat")
    if not re.search(r"8.{0,6}11", dm) or "UAE" not in dm:
        errs.append("dm must offer 8-11 AM UAE time")
    if re.search(r"dealers? (to )?stop losing money you", note + " " + dm, re.I):
        errs.append("grammar: 'dealers ... money you' should be 'they' (or 'help you ... you')")
    if not re.search(r"pilot", dm, re.I):
        errs.append("dm must mention the 30-day free pilot")
    return errs


def draft_prompt(r, feedback=""):
    role = ("an automotive leader" if r["category"] == "automotive" else
            "an investor / ecosystem leader (ask for advice and perspective, not a sale)"
            if r["category"] == "investor" else "a business leader")
    fb = f"\nYour previous attempt had these problems, fix all of them: {feedback}\n" if feedback else ""
    return f"""{VOICE}

Recipient: {r['name']}, {r['title']} at {r['company']} ({r['city']}). They are {role}.
The one real detail you may reference (from a public source): {r['detail'] or r['why']}
{fb}
Write two messages and return JSON with keys "note" and "dm".
"note": a LinkedIn connection note, UNDER 260 characters total. Greet by first name, reference the detail
briefly, say who Rudra is in a few words, and include the idea of helping dealers "stop losing money they
already earned". Ask only to connect.
"dm": a follow-up message for after they accept, 70-120 words. Open with thanks for connecting, then lead
with "stop losing money you already earned" in a natural sentence, explain DealerScope in one sentence
(lost leads, missed service visits, lease-ends, from their DMS/CRM export), mention the 30-day free pilot
lightly, and ask for a 20-minute virtual coffee chat at a time that suits them between 8 and 11 AM UAE time.
Sign off as "Rudra". No subject line."""


def draft_one(r, force=False):
    path = os.path.join(DRAFTS, slug(r["name"]) + ".json")
    if os.path.exists(path) and not force:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    feedback, out = "", {}
    for attempt in range(4):
        try:
            out = ollama(draft_prompt(r, feedback), temperature=0.5 + 0.1 * attempt)
        except Exception as e:  # noqa: BLE001
            feedback = f"invalid output ({e})"
            continue
        note, dm = str(out.get("note", "")).strip(), str(out.get("dm", "")).strip()
        errs = problems(note, dm)
        if not errs:
            break
        feedback = "; ".join(errs)
    note, dm = str(out.get("note", "")).strip(), str(out.get("dm", "")).strip()
    res = {"name": r["name"], "note": note, "dm": dm, "model": MODEL, "created": today(),
           "issues": problems(note, dm), "note_chars": len(note)}
    ensure_dirs()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)
    os.chmod(path, 0o600)
    return res


def load_draft(r):
    path = os.path.join(DRAFTS, slug(r["name"]) + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return None


def cmd_draft(args):
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    rows = load_tracker()
    if args and args[0] != "--all":
        targets = find(rows, " ".join(args))
    else:
        targets = [r for r in rows if r["status"] == "to-send"]
    for r in targets:
        print(f"drafting {r['name']} ...", flush=True)
        d = draft_one(r, force)
        flag = "OK" if not d["issues"] else "CHECK: " + "; ".join(d["issues"])
        print(f"  note ({d['note_chars']} chars) [{flag}]\n  {d['note']}\n")
    return 0


# ---------------------------------------------------------------- 4. tracker commands

def sent_today(rows):
    return sum(1 for r in rows if r.get("sent_date") == today())


def cmd_today(args):
    rows = load_tracker()
    done = sent_today(rows)
    room = DAILY_CAP - done
    if room <= 0:
        print(f"You've marked {done} sent today, which is the daily cap of {DAILY_CAP}. Come back tomorrow.")
        return 0
    queue = [r for r in rows if r["status"] == "to-send"]
    queue.sort(key=lambda r: -int(r["score"] or 0))
    batch = queue[:min(TODAY_N, room)]
    print(f"Today's batch: {len(batch)} (sent today: {done}/{DAILY_CAP}; {len(queue)} waiting in total)\n")
    for i, r in enumerate(batch, 1):
        d = load_draft(r)
        print(f"{i:>2}. {r['name']} | {r['title']}, {r['company']} | score {r['score']}")
        print(f"    why:    {r['why']}")
        print(f"    source: {r['source_url']}  (verified: {r['verified'] or 'not yet'})")
        print(f"    search: {r['linkedin_search']}")
        if d:
            warn = "" if not d["issues"] else "  <- CHECK: " + "; ".join(d["issues"])
            print(f"    note ({d['note_chars']}c):{warn}\n      {d['note']}")
        else:
            print(f"    note:   (no draft yet; run: prospect draft \"{r['name']}\")")
        print()
    print('After sending by hand: prospect mark "<name>" sent')
    return 0


def cmd_mark(args):
    if len(args) < 2:
        print('usage: prospect mark "<name>" <status> [--note "text"]   statuses: ' + ", ".join(STATUSES))
        return 1
    note = ""
    if "--note" in args:
        i = args.index("--note")
        note, args = " ".join(args[i + 1:]), args[:i]
    # Status may be two words ("chat booked"): try the longest valid suffix.
    status = None
    for n in (2, 1):
        cand = " ".join(args[-n:]).lower()
        if len(args) > n and cand in STATUSES:
            status, name = cand, " ".join(args[:-n])
            break
    if not status:
        print("unknown status. use one of: " + ", ".join(STATUSES))
        return 1
    rows = load_tracker()
    hits = find(rows, name)
    if len(hits) != 1:
        print(f"{len(hits)} matches for '{name}': " + ", ".join(h["name"] for h in hits[:8]))
        return 1
    r = hits[0]
    if status == "sent" and r["status"] == "to-send" and sent_today(rows) >= DAILY_CAP:
        print(f"Warning: that's more than {DAILY_CAP} sent today. Recorded anyway; slow down tomorrow.")
    r["status"] = status
    col = {"sent": "sent_date", "accepted": "accepted_date", "chat booked": "chat_date"}.get(status)
    if col:
        r[col] = today()
    if note:
        r["notes"] = (r["notes"] + " | " if r["notes"] else "") + f"{today()}: {note}"
    save_tracker(rows)
    print(f"{r['name']} -> {status}")
    if status == "accepted":
        d = load_draft(r)
        if d:
            print("\nFollow-up DM to send by hand:\n\n" + d["dm"])
    return 0


def cmd_list(args):
    rows = load_tracker()
    if args:
        rows = [r for r in rows if r["status"] == " ".join(args)]
    for r in rows:
        print(f"{r['score']:>3}  {r['status']:<11} {r['name']:<28} {r['title'][:30]:<30} {r['company']}")
    print(f"\n{len(rows)} rows")
    return 0


def cmd_add(args):
    # prospect add "Name" "Title" "Company" "City" "source_url" "why" ["detail"]
    if len(args) < 6:
        print('usage: prospect add "Name" "Title" "Company" "City" "source_url" "why" ["detail"]')
        return 1
    name, title, company, city, url, why = args[:6]
    detail = args[6] if len(args) > 6 else ""
    if not url.startswith("http"):
        print("A public source URL is required.")
        return 1
    cat = categorize(company, title)
    rows = load_tracker()
    upsert(rows, [{"name": name, "title": title, "company": company, "city": city, "category": cat,
                   "score": str(score(cat, title, company)), "source": "public-web", "source_url": url,
                   "why": why, "detail": detail, "linkedin_search": linkedin_search(name, company)}])
    save_tracker(rows)
    print(f"added {name} (score {score(cat, title, company)}). Run: prospect verify \"{name}\"")
    return 0


# ---------------------------------------------------------------- 5. prep brief

def cmd_prep(args):
    if not args:
        print('usage: prospect prep "<name>"')
        return 1
    rows = load_tracker()
    hits = find(rows, " ".join(args))
    if len(hits) != 1:
        print(f"{len(hits)} matches: " + ", ".join(h["name"] for h in hits[:8]))
        return 1
    r = hits[0]
    research = {}
    if os.path.exists(RESEARCH):
        with open(RESEARCH, encoding="utf-8") as f:
            research = {p["name"]: p for p in json.load(f)}
    p = research.get(r["name"], {})
    prompt = f"""You help Rudra (18, UCSD, founder of DealerScope) prepare for a 20-minute coffee chat.
DealerScope finds revenue a dealership is losing (lost leads, missed service visits, lease-ends) from its
DMS/CRM export, offered as a 30-day free pilot.
Person: {r['name']}, {r['title']}, {r['company']} ({r['city']}). Category: {r['category']}.
Known facts (from {r['source_url']}): {r['detail']} {r['why']}
Brands: {p.get('brands', 'unknown')}. Locations: {p.get('locations', 'unknown')}.
Return JSON with keys:
"pains": list of 3 short likely pains, phrased as hypotheses to test ("may", "likely"), specific to this
kind of business, not claims;
"questions": list of 3 open, curious questions Rudra should ask;
"pilot_ask": 2-3 sentences, humble, asking whether a 30-day free pilot on one rooftop or one brand using a
DMS/CRM export would be worth trying, and what data access it would need. For an investor, ask for advice
and one intro to a dealer group instead. No percentages, no hype, no exclamation marks."""
    try:
        out = ollama(prompt, temperature=0.4)
    except Exception as e:  # noqa: BLE001
        print(f"local model unavailable ({e}); writing facts-only brief")
        out = {}

    def bullets(xs):
        return "\n".join(f"- {x}" for x in (xs or ["(fill in)"]))
    md = f"""# Coffee-chat brief: {r['name']}

**{r['title']}, {r['company']}** · {r['city']} · score {r['score']} · status {r['status']}
Generated {today()} on local model {MODEL}. Facts come from the source; pains and questions are guesses to test.

## Who they are
{r['why']}

Verified detail: {r['detail'] or '(none recorded)'}
Source: {r['source_url']} ({r['source_date'] or 'undated'}). Verification: {r['verified'] or 'not run'}
Profile search (open yourself): {r['linkedin_search']}

## Company: brands and locations
- Brands: {p.get('brands') or 'check source'}
- Locations: {p.get('locations') or 'check source'}

## Likely pains (hypotheses)
{bullets(out.get('pains'))}

## 3 questions to ask
{bullets(out.get('questions'))}

## The pilot ask
{out.get('pilot_ask', '(fill in)')}

## Before the call
- Re-read the source page; confirm the title hasn't changed.
- Time: 8 to 11 AM UAE = 8 to 11 PM PST (Nov-Mar) or 9 PM to midnight PDT, the night before.
- Notes from the chat go in: prospect mark "{r['name']}" <status> --note "..."
"""
    ensure_dirs()
    path = os.path.join(BRIEFS, slug(r["name"]) + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    os.chmod(path, 0o600)
    print(md)
    print(f"saved: {path}")
    return 0


# ---------------------------------------------------------------- main

HELP = """prospect <command>
  parse [zip]          import UAE people from your newest LinkedIn Connections export
  research [json]      import researched public-source people (default data/research.json)
  add "Name" "Title" "Company" "City" "url" "why" ["detail"]   add one person by hand
  verify [name|--all]  re-check each source page shows the person's name and title
  draft [name|--all] [--force]   draft note + follow-up DM on local Ollama
  today                top 10 un-contacted people with note + profile-search link (cap 15/day)
  mark "<name>" <status> [--note "..."]   statuses: to-send, sent, accepted, chat booked, pilot, no
  prep "<name>"        write a one-page coffee-chat brief to data/briefs/
  list [status]        show the tracker"""

COMMANDS = {"parse": cmd_parse, "research": cmd_research, "verify": cmd_verify, "draft": cmd_draft,
            "today": cmd_today, "mark": cmd_mark, "prep": cmd_prep, "list": cmd_list, "add": cmd_add}


def main(argv):
    ensure_dirs()
    if not argv or argv[0] not in COMMANDS:
        print(HELP)
        return 0 if not argv else 1
    return COMMANDS[argv[0]](argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
