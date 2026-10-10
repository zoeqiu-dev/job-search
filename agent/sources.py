"""
Job sourcing. Every source here is either an official API, a public ATS
endpoint the employer publishes for exactly this purpose, or mail sent to you.
Nothing here scrapes LinkedIn or Indeed directly. See README section 2.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

import requests

UA = {"User-Agent": "personal-job-agent/1.0"}
TIMEOUT = 30


@dataclass
class Job:
    title: str
    company: str
    location: str = ""
    url: str = ""
    description: str = ""
    salary: str = ""            # as stated by the source, for display
    salary_min: int | None = None   # normalised to annual, for filtering
    salary_max: int | None = None
    salary_estimated: bool = False  # True when the source predicted it
    posted_at: str = ""
    source: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.salary_min is None and self.salary:
            self.salary_min, self.salary_max = parse_salary(self.salary)

    @property
    def fingerprint(self) -> str:
        """Stable ID for dedupe across sources. Same role on Adzuna and
        Google Jobs collapses to one entry."""
        key = f"{_norm_company(self.company)}|{_norm(self.title)}|{_norm(self.location)[:12]}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def as_dict(self) -> dict:
        return asdict(self)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# Aggregators disagree on legal suffixes: "Acme", "ACME Inc.", "Acme, LLC" are
# one employer. Strip them before fingerprinting or dedupe silently fails.
_SUFFIXES = r"(inc|llc|ltd|limited|corp|corporation|co|plc|gmbh|sa|nv|ag|group|holdings|technologies|technology|labs|inc\.)"


def _norm_company(s: str) -> str:
    t = (s or "").lower()
    t = re.sub(r"[^a-z0-9\s]+", " ", t)
    t = re.sub(rf"\b{_SUFFIXES}\b", " ", t)
    t = re.sub(r"^\s*the\b", " ", t)
    return re.sub(r"\s+", "", t)


# Sources state pay in wildly different shapes: "$150K–$200K a year",
# "$75 an hour", "Up to £90,000". Normalise everything to an annual number so a
# single threshold in config.yaml can filter across all of them.
_PERIOD_MULTIPLIER = [
    # (pattern, annual multiplier, largest raw figure that plausibly means this
    # period). Patterns are deliberately strict: a bare "hr" is usually HR the
    # department, and "5 days a week" is a schedule, not a weekly rate.
    (r"(per\s+hour|an\s+hour|/\s*hour|/\s*hr\b|\bhourly)", 2080, 1_000),
    (r"(per\s+day|/\s*day\b|\bdaily\s+rate|\bday\s+rate)", 260, 5_000),
    (r"(per\s+week|/\s*week\b|/\s*wk\b|\bweekly)", 52, 20_000),
    (r"(per\s+month|a\s+month|/\s*month\b|/\s*mo\b|\bmonthly)", 12, 50_000),
]


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """Returns (annual_min, annual_max). Either may be None."""
    if not text:
        return None, None
    t = text.lower().replace(",", "")

    mult, cap = 1, None
    for pattern, m, raw_max in _PERIOD_MULTIPLIER:
        if re.search(pattern, t):
            mult, cap = m, raw_max
            break

    nums: list[float] = []
    for raw, suffix in re.findall(r"(\d+(?:\.\d+)?)\s*([km]?)\b", t):
        v = float(raw)
        if suffix == "k":
            v *= 1_000
        elif suffix == "m":
            v *= 1_000_000
        # A bare number under 1000 with no period marker is noise (a year, a
        # headcount) unless we've established an hourly/daily rate.
        if v < 1000 and mult == 1:
            continue
        # "$150k base + monthly bonus": a figure already too big to be a
        # monthly rate is annual, whatever else the text says.
        nums.append(v * mult if cap is None or v <= cap else v)

    nums = [n for n in nums if 10_000 <= n <= 10_000_000]
    if not nums:
        return None, None
    lo, hi = int(min(nums)), int(max(nums))
    if re.search(r"\b(up to|max|maximum|under)\b", t):
        return None, hi
    if re.search(r"\b(from|starting at|at least|minimum|min)\b", t) and lo == hi:
        return lo, None
    return lo, hi


def _get(url: str, **kw) -> requests.Response | None:
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  ! {url.split('?')[0]} failed: {e}")
                return None
            time.sleep(1.5 ** attempt)
    return None


def queries(cfg: dict) -> list[str]:
    """What gets sent to the aggregators: search.keywords plus, when
    search.keywords_from_titles is on, every target title. So changing your
    titles changes the search, not just the filter. Case-insensitive dedupe."""
    s = cfg.get("search") or {}
    qs = list(s.get("keywords") or [])
    if s.get("keywords_from_titles", True):
        qs += (cfg.get("candidate") or {}).get("target_titles") or []
    seen, out = set(), []
    for q in qs:
        k = " ".join(q.lower().replace(",", " ").split())
        if k and k not in seen:
            seen.add(k)
            out.append(q)
    return out


# ── Adzuna ────────────────────────────────────────────────────────────────────
def from_adzuna(cfg: dict) -> list[Job]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        print("  - Adzuna: no credentials, skipping")
        return []

    s, out = cfg["search"], []
    for kw in queries(cfg):
        for loc in s["locations"]:
            remote = loc.lower() == "remote"
            url = f"https://api.adzuna.com/v1/api/jobs/{s['country']}/search/1"
            r = _get(url, params={
                "app_id": app_id, "app_key": app_key,
                # Adzuna has no remote flag; an empty `where` alone returns
                # onsite jobs nationwide. Ask for the word instead.
                "what": f"{kw} remote" if remote else kw,
                "where": "" if remote else loc,
                "results_per_page": 50,         # Adzuna's max; same one call
                "max_days_old": s["max_days_old"],
                "content-type": "application/json",
            })
            if not r:
                continue
            for item in r.json().get("results", []):
                # Adzuna gives numeric bounds plus a flag for whether it modelled
                # them rather than reading them off the posting.
                lo = int(item["salary_min"]) if item.get("salary_min") else None
                hi = int(item["salary_max"]) if item.get("salary_max") else None
                predicted = str(item.get("salary_is_predicted", "0")) == "1"
                sal = ""
                if lo:
                    sal = f"${lo:,}–${hi:,}" if hi and hi != lo else f"${lo:,}"
                    if predicted:
                        sal += " (estimated)"
                out.append(Job(
                    title=item.get("title", "").strip(),
                    company=item.get("company", {}).get("display_name", "").strip(),
                    location=_adzuna_location(item.get("location") or {}),
                    url=item.get("redirect_url", ""),
                    description=re.sub(r"<[^>]+>", "", item.get("description", "")),
                    salary=sal, salary_min=lo, salary_max=hi,
                    salary_estimated=predicted,
                    posted_at=item.get("created", ""),
                    source="adzuna",
                ))
            time.sleep(0.4)
    print(f"  · Adzuna: {len(out)}")
    return out


def _adzuna_location(loc: dict) -> str:
    """Adzuna's display_name can omit the state ("Santa Monica, Los Angeles
    County"); its `area` list has it (["US", "California", ...])."""
    name = loc.get("display_name", "")
    area = loc.get("area") or []
    if len(area) > 1 and area[1].lower() not in name.lower():
        name = f"{name}, {area[1]}" if name else area[1]
    return name


# ── Google Jobs (SerpApi) ─────────────────────────────────────────────────────
def from_google_jobs(cfg: dict) -> list[Job]:
    """Google for Jobs indexes LinkedIn, Indeed, Glassdoor and company boards.
    This is the compliant way to see those postings."""
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        print("  - Google Jobs: no SERPAPI_KEY, skipping")
        return []

    s, out = cfg["search"], []
    for kw in queries(cfg):
        for loc in s["locations"]:
            remote = loc.lower() == "remote"
            r = _get("https://serpapi.com/search", params={
                "engine": "google_jobs", "q": kw,
                # SerpApi wants a canonical place: "California" alone is
                # ambiguous, "California, United States" isn't.
                "location": None if remote else (loc if "," in loc else f"{loc}, United States"),
                "ltype": "1" if remote else None,
                "gl": s.get("country", "us"), "hl": "en", "api_key": key,
            })
            if not r:
                continue
            for item in r.json().get("jobs_results", []):
                opts = item.get("apply_options") or []
                det = item.get("detected_extensions", {}) or {}
                out.append(Job(
                    title=item.get("title", "").strip(),
                    company=item.get("company_name", "").strip(),
                    location=item.get("location", ""),
                    url=(opts[0].get("link") if opts else item.get("share_link", "")),
                    description=item.get("description", ""),
                    salary=det.get("salary", ""),
                    posted_at=det.get("posted_at", ""),
                    source="google_jobs",
                    extra={"apply_options": [o.get("title") for o in opts]},
                ))
            time.sleep(0.5)
    print(f"  · Google Jobs: {len(out)}")
    return out


# ── ATS boards (free, no key, first-party) ────────────────────────────────────
def from_ats(cfg: dict) -> list[Job]:
    out: list[Job] = []
    ats = cfg.get("ats", {}) or {}

    for token in ats.get("greenhouse", []) or []:
        r = _get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
        if not r:
            continue
        for j in r.json().get("jobs", []):
            out.append(Job(
                title=j.get("title", ""),
                company=token.replace("-", " ").title(),
                location=(j.get("location") or {}).get("name", ""),
                url=j.get("absolute_url", ""),
                description=_unescape(j.get("content", "")),
                posted_at=j.get("updated_at", ""),
                source=f"greenhouse:{token}",
            ))

    for token in ats.get("lever", []) or []:
        r = _get(f"https://api.lever.co/v0/postings/{token}?mode=json")
        if not r:
            continue
        for j in r.json():
            out.append(Job(
                title=j.get("text", ""),
                company=token.replace("-", " ").title(),
                location=(j.get("categories") or {}).get("location", ""),
                url=j.get("hostedUrl", ""),
                description=_lever_description(j),
                posted_at=datetime.fromtimestamp(
                    j.get("createdAt", 0) / 1000, tz=timezone.utc).isoformat() if j.get("createdAt") else "",
                source=f"lever:{token}",
            ))

    for token in ats.get("ashby", []) or []:
        r = _get(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true")
        if not r:
            continue
        for j in r.json().get("jobs", []):
            comp = j.get("compensation") or {}
            summaries = comp.get("summaryComponents") or []
            out.append(Job(
                title=j.get("title", ""),
                company=token.replace("-", " ").title(),
                location=j.get("location", ""),
                url=j.get("jobUrl", ""),
                description=re.sub(r"<[^>]+>", " ", j.get("descriptionHtml", "")),
                salary=comp.get("compensationTierSummary") or (
                    summaries[0].get("summary", "") if summaries else ""),
                posted_at=j.get("publishedAt", ""),
                source=f"ashby:{token}",
            ))

    print(f"  · ATS boards: {len(out)}")
    return out


def _lever_description(j: dict) -> str:
    """Lever puts the intro in descriptionPlain and the actual requirements
    ("What you'll need") in `lists`. Without the lists the rule screen has no
    years or degree requirements to read."""
    parts = [j.get("descriptionPlain") or j.get("description", "")]
    for lst in j.get("lists") or []:
        parts.append(f"\n{lst.get('text', '')}\n{lst.get('content', '')}")
    parts.append(j.get("additionalPlain") or j.get("additional", ""))
    return _unescape("\n".join(p for p in parts if p))


def _unescape(s: str) -> str:
    import html
    return re.sub(r"<[^>]+>", " ", html.unescape(s or ""))


# ── LinkedIn / Indeed job-alert emails ────────────────────────────────────────
def from_alert_emails(cfg: dict, lookback_days: int = 2) -> list[Job]:
    """Reads job-alert emails LinkedIn/Indeed sent *to you* and pulls the links.

    This is not scraping: it's your own mailbox. Set up the alerts in the
    LinkedIn or Indeed UI first, then point this at the inbox. The trade-off is
    that alert emails carry title/company/location but no job description, so
    the pipeline fetches less context for these than for API sources.
    """
    user, pw = os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASSWORD")
    if not (user and pw):
        print("  - Alert emails: no IMAP credentials, skipping")
        return []

    out: list[Job] = []
    try:
        m = imaplib.IMAP4_SSL(os.environ.get("IMAP_HOST", "imap.gmail.com"))
        m.login(user, pw)
        m.select("INBOX")
        since = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
        typ, data = m.search(None, f'(SINCE {since} FROM "jobalerts-noreply@linkedin.com")')
        for uid in (data[0].split() if data and data[0] else []):
            typ, raw = m.fetch(uid, "(RFC822)")
            msg = email.message_from_bytes(raw[0][1])
            body = _body_of(msg)
            for url, title in re.findall(
                    r'href="(https://www\.linkedin\.com/comm/jobs/view/[^"]+)"[^>]*>\s*([^<]{4,120})<', body):
                out.append(Job(
                    title=title.strip(),
                    company="(from LinkedIn alert — confirm on page)",
                    url=url.split("?")[0],
                    source="linkedin_alert_email",
                ))
        m.logout()
    except Exception as e:
        print(f"  ! alert email read failed: {e}")

    print(f"  · Alert emails: {len(out)}")
    return out


def _body_of(msg) -> str:
    if not msg.is_multipart():
        return msg.get_payload(decode=True).decode("utf-8", "ignore")
    parts = []
    for p in msg.walk():
        if p.get_content_type() in ("text/html", "text/plain"):
            payload = p.get_payload(decode=True)
            if payload:
                parts.append(payload.decode("utf-8", "ignore"))
    return "\n".join(parts)


# ── Orchestration ─────────────────────────────────────────────────────────────
def collect(cfg: dict) -> list[Job]:
    src = cfg.get("sources", {})
    jobs: list[Job] = []
    if src.get("adzuna") or src.get("google_jobs"):
        q, locs = len(queries(cfg)), len((cfg.get("search") or {}).get("locations") or [])
        print(f"  {q} queries × {locs} locations = {q * locs} calls per aggregator per run "
              f"(~{q * locs * 30}/month at one run a day — check your SerpApi plan)")
    if src.get("adzuna"):
        jobs += from_adzuna(cfg)
    if src.get("google_jobs"):
        jobs += from_google_jobs(cfg)
    if src.get("ats_boards"):
        jobs += from_ats(cfg)
    if src.get("linkedin_alert_email"):
        jobs += from_alert_emails(cfg)
    return dedupe(apply_filters(jobs, cfg))


def apply_filters(jobs: Iterable[Job], cfg: dict) -> list[Job]:
    f = cfg.get("filters", {}) or {}
    s = cfg.get("search", {}) or {}
    # Whole words only: "intern" must not drop "Internal Tools" or
    # "International Payments", nor "contract" drop "Smart Contracts".
    bad_title = [_word_re(t) for t in f.get("exclude_title_terms", []) or []]
    bad_co = [_norm_company(c) for c in f.get("exclude_companies", []) or []]
    need = [_word_re(t) for t in f.get("require_terms", []) or []]
    where_ok = _location_rule(cfg)

    floor = s.get("min_salary") or 0
    # Most postings state no pay at all. Dropping those would discard the
    # majority of real openings, so "keep" is the sane default.
    unknown_policy = (s.get("salary_unknown") or "keep").lower()
    trust_estimates = bool(s.get("trust_estimated_salary", False))

    kept, dropped_pay, dropped_loc = [], 0, 0
    for j in jobs:
        t = (j.title or "").lower()
        if not j.title or not j.url:
            continue
        if any(b.search(t) for b in bad_title):
            continue
        if _norm_company(j.company) in bad_co:
            continue
        if need and not any(n.search(t) for n in need):
            continue
        if where_ok and not where_ok(j):
            dropped_loc += 1
            continue

        if floor:
            ok, known = _clears_salary(j, floor, trust_estimates)
            if not ok:
                dropped_pay += 1
                continue
            if not known and unknown_policy == "drop":
                dropped_pay += 1
                continue

        kept.append(j)

    if dropped_pay:
        print(f"  · dropped {dropped_pay} on the salary floor (${floor:,})")
    if dropped_loc:
        state = ((cfg.get("location") or {}).get("state") or "").upper()
        print(f"  · dropped {dropped_loc} postings not in {state}")
    return kept


def _word_re(term: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])")


# ── Location: is this posting in the state you want? ─────────────────────────
# Applied to every source. Aggregators can't be trusted to have done it: a
# "Remote" query to Adzuna returns onsite jobs nationwide, and ATS boards list
# every office the company has.

US_STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "dc": "district of columbia",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho", "il": "illinois",
    "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana",
    "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan",
    "mn": "minnesota", "ms": "mississippi", "mo": "missouri", "mt": "montana",
    "ne": "nebraska", "nv": "nevada", "nh": "new hampshire", "nj": "new jersey",
    "nm": "new mexico", "ny": "new york", "nc": "north carolina", "nd": "north dakota",
    "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee",
    "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
}

# Postings often name only the city. Ambiguous names (Newark, Richmond,
# Concord, Dublin, Ontario, Carson…) are left out; "City, CA" still matches.
STATE_CITIES = {"ca": """
    los angeles|san francisco|san jose|san diego|oakland|sacramento|fresno|long beach|
    irvine|santa monica|pasadena|burbank|glendale|culver city|el segundo|playa vista|
    marina del rey|west hollywood|beverly hills|century city|torrance|manhattan beach|
    redondo beach|hawthorne|inglewood|downey|whittier|covina|west covina|pomona|
    rancho cucamonga|riverside|san bernardino|anaheim|santa ana|costa mesa|
    newport beach|huntington beach|fullerton|tustin|lake forest|aliso viejo|
    mission viejo|laguna hills|laguna beach|orange county|inland empire|
    thousand oaks|westlake village|calabasas|woodland hills|sherman oaks|studio city|
    north hollywood|van nuys|chatsworth|valencia|santa clarita|simi valley|ventura|
    oxnard|camarillo|santa barbara|goleta|san luis obispo|bakersfield|carlsbad|
    la jolla|del mar|oceanside|escondido|temecula|chula vista|palo alto|menlo park|
    mountain view|sunnyvale|santa clara|cupertino|redwood city|redwood shores|
    san mateo|foster city|burlingame|south san francisco|san bruno|millbrae|
    san carlos|los altos|los gatos|campbell|milpitas|fremont|union city|hayward|
    san leandro|alameda|emeryville|berkeley|walnut creek|pleasanton|livermore|
    san ramon|santa rosa|petaluma|novato|san rafael|sausalito|mill valley|
    santa cruz|scotts valley|monterey|davis|roseville|folsom|rancho cordova|
    stockton|modesto|bay area|silicon valley|socal|norcal""",
}

_FOREIGN = ["uk", "united kingdom", "england", "london", "emea", "europe", "eu",
            "canada", "toronto", "vancouver", "montreal", "ottawa", "waterloo",
            "india", "bangalore", "bengaluru", "hyderabad", "apac", "germany", "berlin",
            "munich", "ireland", "dublin", "australia", "sydney", "melbourne",
            "singapore", "japan", "tokyo", "brazil", "sao paulo", "mexico", "latam",
            "france", "paris", "netherlands", "amsterdam", "spain", "madrid",
            "barcelona", "poland", "warsaw", "israel", "tel aviv", "korea", "seoul",
            "switzerland", "zurich", "sweden", "stockholm", "denmark", "copenhagen",
            "china", "hong kong", "taiwan", "philippines", "argentina", "colombia"]
# Canadian province codes, so "Vancouver, BC" or "Toronto, ON, CA" isn't California.
_CA_PROVINCES = r",\s*(on|bc|qc|ab|mb|ns|nb|nl|pe|sk)\b"
_REMOTE = r"\b(remote|anywhere|distributed|work from home|wfh|virtual)\b"
_US = r"\b(us|usa|u\.s\.?|united states|north america|americas)\b"


def _word_re(term: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])")


def _states_named(part: str) -> set[str]:
    """US state codes a location string names: ", NY", "New York", "Texas"."""
    found = set()
    for code, name in US_STATES.items():
        # Codes only after a comma or dash ("Austin, TX", "Remote - NY") —
        # bare "in", "or", "me" are ordinary words.
        if re.search(rf"(,|\s-|\()\s*{code}\b", part) or _word_re(name).search(part):
            found.add(code)
    if "west virginia" in part:
        found.discard("va")
    if "washington" in part and re.search(r"washington,?\s*d\.?c", part):
        found.discard("wa"); found.add("dc")
    return found


def _foreign(part: str) -> bool:
    if re.search(_CA_PROVINCES, part):
        return True
    return any(_word_re(x).search(part) for x in _FOREIGN if not (
        x == "mexico" and "new mexico" in part))


def location_verdict(location: str, state: str = "ca", extra_cities=(),
                     allow_remote: bool = True) -> str:
    """'in' | 'remote' | 'out' | 'unknown' for one posting's location string.

    Multi-office strings ("San Francisco, CA; New York, NY") count as 'in' if
    any office is in the state. Remote counts only if it isn't remote
    somewhere else ("Remote - UK") or limited to other states ("Remote - NY").
    """
    state = state.lower()
    loc = (location or "").lower().strip()
    if not loc:
        return "unknown"
    cities = [c.strip() for c in STATE_CITIES.get(state, "").replace("\n", "").split("|") if c.strip()]
    cities += [c.lower() for c in extra_cities]

    # "Santa Clara or San Jose" is two places; "Portland, OR" is Oregon.
    parts = [p.strip() for p in re.split(r";|\||\n|\s/\s|(?<!,)\s+or\s+|\+\s*\d+\s+others?|•", loc)
             if p.strip()]
    saw_place = False
    remote_ok = False
    for part in parts:
        named = _states_named(part)
        foreign = _foreign(part)
        if named or foreign:
            saw_place = True
        if state in named and not foreign:
            return "in"
        if not named and not foreign and any(_word_re(c).search(part) for c in cities):
            return "in"
        if re.search(_REMOTE, part):
            saw_place = True
            if foreign and not re.search(_US, part):
                continue
            if named and state not in named:
                continue  # "Remote - New York only"
            remote_ok = True
    if remote_ok and allow_remote:
        return "remote"
    return "out" if saw_place or remote_ok else "unknown"


def _location_rule(cfg: dict):
    """Returns f(job) -> bool, or None when the check is off."""
    lc = cfg.get("location") or {}
    state = (lc.get("state") or "").lower()
    if not state:
        return None
    state = next((k for k, v in US_STATES.items() if state in (k, v)), state)
    allow_remote = lc.get("allow_remote", True)
    unknown = (lc.get("unknown") or "drop").lower()
    extra = lc.get("extra_cities") or []

    def ok(j: Job) -> bool:
        v = location_verdict(j.location, state, extra, allow_remote)
        j.extra["location_verdict"] = v
        if v == "unknown":
            # Alert emails carry no location; the alert itself was set up with one.
            return unknown == "keep" or j.source == "linkedin_alert_email"
        return v in ("in", "remote")
    return ok


def _clears_salary(j: Job, floor: int, trust_estimates: bool) -> tuple[bool, bool]:
    """(passes_filter, salary_was_stated).

    Judged on the TOP of the band: a posting advertising $160k–$200k still
    clears a $180k floor, because that's a range you can negotiate within.
    """
    if j.salary_estimated and not trust_estimates:
        return True, False          # treat a modelled figure as unknown
    top = j.salary_max or j.salary_min
    if top is None:
        return True, False
    return top >= floor, True


def dedupe(jobs: list[Job]) -> list[Job]:
    """Prefer the copy with the longest description — usually the ATS or
    Adzuna record rather than an alert-email stub."""
    best: dict[str, Job] = {}
    for j in jobs:
        cur = best.get(j.fingerprint)
        if cur is None or len(j.description) > len(cur.description):
            best[j.fingerprint] = j
    return list(best.values())
