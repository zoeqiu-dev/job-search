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


def search_areas(cfg: dict) -> list[dict]:
    """search.locations as {"name", "near", "radius_km", "remote"} dicts.
    Plain strings still work: "Remote", or a place searched at its default radius."""
    out = []
    for loc in (cfg.get("search") or {}).get("locations") or []:
        if isinstance(loc, str):
            remote = loc.strip().lower() == "remote"
            out.append({"name": loc, "near": "" if remote else loc, "radius_km": None,
                        "remote": remote})
        else:
            out.append({"name": loc.get("name") or loc.get("near"), "near": loc.get("near", ""),
                        "radius_km": loc.get("radius_km"), "remote": False})
    return out


class Rotation:
    """Spreads a fixed number of calls per run across every query × area
    combination, picking up where the last run stopped. Free tiers can't
    afford every combination daily; this covers them all every few runs.
    `store` needs get_kv/set_kv (state.db); without one it starts at 0."""

    def __init__(self, store=None, save: bool = True):
        self.store, self.save = store, save

    def plan(self, source: str, combos: list, per_run: int | None) -> list:
        if not per_run or per_run >= len(combos):
            return combos
        has_store = self.store is not None
        start = int(self.store.get_kv(f"rotation:{source}") or 0) % len(combos) if has_store else 0
        picked = [combos[(start + i) % len(combos)] for i in range(per_run)]
        if has_store and self.save:
            self.store.set_kv(f"rotation:{source}", str((start + per_run) % len(combos)))
        runs = -(-len(combos) // per_run)
        print(f"  · {source}: {per_run} of {len(combos)} query×area searches this run "
              f"(all covered every {runs} runs)")
        return picked


def _combos(cfg: dict) -> list[tuple[str, dict]]:
    # Query-major, so one run searches the same query in every area.
    return [(q, a) for q in queries(cfg) for a in search_areas(cfg)]


# ── Adzuna ────────────────────────────────────────────────────────────────────
def from_adzuna(cfg: dict, rotation: Rotation | None = None) -> list[Job]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        print("  - Adzuna: no credentials, skipping")
        return []

    s, out = cfg["search"], []
    per_run = (cfg.get("sources") or {}).get("adzuna_calls_per_run")
    for kw, area in (rotation or Rotation()).plan("adzuna", _combos(cfg), per_run):
        remote = area["remote"]
        url = f"https://api.adzuna.com/v1/api/jobs/{s['country']}/search/1"
        r = _get(url, params={
            "app_id": app_id, "app_key": app_key,
            # Adzuna has no remote flag; an empty `where` alone returns
            # onsite jobs nationwide. Ask for the word instead.
            "what": f"{kw} remote" if remote else kw,
            "where": "" if remote else area["near"],
            # Adzuna's default radius is a few km: "Los Angeles" alone
            # misses Santa Monica and Irvine.
            "distance": area["radius_km"],
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
def _serpapi_place(near: str) -> str:
    """SerpApi wants canonical names: "San Francisco, CA" →
    "San Francisco, California, United States"."""
    parts = [p.strip() for p in near.split(",")]
    if len(parts) == 2 and parts[1].lower() in US_STATES:
        parts[1] = US_STATES[parts[1].lower()].title()
    if parts[-1].lower() not in ("united states", "usa", "us"):
        parts.append("United States")
    return ", ".join(parts)


def from_google_jobs(cfg: dict, rotation: Rotation | None = None) -> list[Job]:
    """Google for Jobs indexes LinkedIn, Indeed, Glassdoor and company boards.
    This is the compliant way to see those postings."""
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        print("  - Google Jobs: no SERPAPI_KEY, skipping")
        return []

    s, out = cfg["search"], []
    per_run = (cfg.get("sources") or {}).get("google_jobs_calls_per_run")
    for kw, area in (rotation or Rotation()).plan("google_jobs", _combos(cfg), per_run):
        remote = area["remote"]
        r = _get("https://serpapi.com/search", params={
            "engine": "google_jobs", "q": kw,
            "location": "United States" if remote else _serpapi_place(area["near"]),
            "lrad": None if remote else area["radius_km"],
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
def collect(cfg: dict, rotation: Rotation | None = None) -> list[Job]:
    """Every source here searches any company: Adzuna and Google Jobs by title
    and area; the ATS boards add the specific companies listed in config."""
    src = cfg.get("sources", {})
    jobs: list[Job] = []
    if src.get("adzuna"):
        jobs += from_adzuna(cfg, rotation)
    if src.get("google_jobs"):
        jobs += from_google_jobs(cfg, rotation)
    if src.get("ats_boards"):
        jobs += from_ats(cfg)
    if src.get("linkedin_alert_email"):
        jobs += from_alert_emails(cfg)
    return dedupe(apply_filters(jobs, cfg))


# ── Staffing agencies ─────────────────────────────────────────────────────────
# Searching any company brings in agencies reposting the same jobs, usually
# without naming the employer. Recognised by name, or by how the ad is written.
AGENCIES = [
    "robert half", "teksystems", "insight global", "randstad", "adecco", "kforce",
    "aerotek", "actalent", "allegis", "apex systems", "motion recruitment", "jobot",
    "cybercoders", "hays", "kelly services", "manpower", "manpowergroup", "experis",
    "akkodis", "modis", "collabera", "beacon hill", "vaco", "creative circle", "aquent",
    "russell tobin", "diverse lynx", "lancesoft", "pyramid consulting", "mindlance",
    "judge group", "yoh", "volt", "signature consultants", "staffmark", "harnham",
    "mondo", "addison group", "solomon page", "korn ferry", "michael page", "page group",
    "talentbridge", "hirequest", "planet professional", "system one", "rht",
    "the mom project", "toptal", "dice", "ziprecruiter", "lensa", "jobs via dice",
    "jobright", "jooble", "talent.com",
]
_AGENCY_NAME = re.compile(
    r"\b(staffing|recruit(ing|ers?|ment)|talent (solutions|partners|acquisition group)|"
    r"search (partners|group)|headhunt\w*|workforce solutions)\b", re.I)
_AGENCY_AD = re.compile(
    r"\b(on behalf of (our|a|my) client|our client,? (a|an|is|the)|my client,? (a|an|is)|"
    r"we are (currently )?(partnering|working) with (a|an|our) client|confidential client)\b",
    re.I)


def is_agency(j: Job, extra_names=()) -> bool:
    co = _norm_company(j.company)
    names = [_norm_company(n) for n in [*AGENCIES, *extra_names]]
    if co and any(co == n for n in names if n):
        return True
    if _AGENCY_NAME.search(j.company or ""):
        return True
    return bool(_AGENCY_AD.search((j.description or "")[:3000]))


def apply_filters(jobs: Iterable[Job], cfg: dict) -> list[Job]:
    f = cfg.get("filters", {}) or {}
    s = cfg.get("search", {}) or {}
    # Whole words only: "intern" must not drop "Internal Tools" or
    # "International Payments", nor "contract" drop "Smart Contracts".
    bad_title = [_word_re(t) for t in f.get("exclude_title_terms", []) or []]
    bad_co = [_norm_company(c) for c in f.get("exclude_companies", []) or []]
    need = [_word_re(t) for t in f.get("require_terms", []) or []]
    where_ok = _location_rule(cfg)
    drop_agencies = f.get("drop_staffing_agencies", True)
    agency_extra = f.get("extra_agencies") or []

    floor = s.get("min_salary") or 0
    # Most postings state no pay at all. Dropping those would discard the
    # majority of real openings, so "keep" is the sane default.
    unknown_policy = (s.get("salary_unknown") or "keep").lower()
    trust_estimates = bool(s.get("trust_estimated_salary", False))

    kept, dropped_pay, dropped_loc, dropped_agency = [], 0, 0, 0
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
        if drop_agencies and is_agency(j, agency_extra):
            dropped_agency += 1
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
    if dropped_agency:
        print(f"  · dropped {dropped_agency} staffing-agency postings")
    if dropped_loc:
        areas = ", ".join((cfg.get("location") or {}).get("regions") or [])
        print(f"  · dropped {dropped_loc} postings outside {areas} / US-remote")
    return kept


def _word_re(term: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])")


# ── Location: is this posting in one of your areas? ──────────────────────────
# Applied to every source. Aggregators can't be trusted to have done it: their
# radius searches spill over, and ATS boards list every office the company has.

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

# Every city and county in each area, as postings write them. A posting that
# names only "California" or another California city (San Diego, Sacramento)
# is not in an area and is dropped.
REGIONS = {
    # All nine counties: San Francisco, San Mateo, Santa Clara, Alameda,
    # Contra Costa, Marin, Napa, Solano, Sonoma.
    "bay_area": """
        bay area|sf bay area|san francisco bay area|silicon valley|peninsula|south bay|
        east bay|north bay|san francisco|sf|south san francisco|daly city|brisbane|
        san mateo|san bruno|millbrae|burlingame|foster city|belmont|san carlos|
        redwood city|redwood shores|menlo park|east palo alto|atherton|half moon bay|
        palo alto|stanford|mountain view|los altos|sunnyvale|santa clara|cupertino|
        san jose|campbell|los gatos|saratoga|milpitas|morgan hill|gilroy|
        oakland|berkeley|emeryville|alameda|san leandro|hayward|union city|fremont|
        newark|pleasanton|dublin|livermore|san ramon|danville|walnut creek|concord|
        lafayette|orinda|richmond|el cerrito|pleasant hill|martinez|antioch|
        san rafael|novato|mill valley|sausalito|larkspur|corte madera|
        napa|vallejo|fairfield|benicia|vacaville|santa rosa|petaluma|rohnert park|sonoma|
        san francisco county|san mateo county|santa clara county|alameda county|
        contra costa county|marin county|napa county|solano county|sonoma county""",
    # Los Angeles County + Orange County.
    "los_angeles": """
        los angeles|la|greater los angeles|los angeles county|los angeles metropolitan area|
        socal|southern california|downtown los angeles|dtla|santa monica|venice|
        marina del rey|playa vista|playa del rey|culver city|el segundo|manhattan beach|
        hermosa beach|redondo beach|torrance|hawthorne|inglewood|carson|gardena|
        long beach|lakewood|cerritos|downey|norwalk|whittier|west hollywood|hollywood|
        beverly hills|century city|westwood|brentwood|pacific palisades|malibu|
        burbank|glendale|pasadena|south pasadena|arcadia|monrovia|alhambra|
        san gabriel|el monte|covina|west covina|azusa|glendora|diamond bar|pomona|
        walnut|city of industry|rowland heights|claremont|la verne|san dimas|
        sherman oaks|studio city|north hollywood|universal city|van nuys|encino|
        woodland hills|calabasas|chatsworth|northridge|canoga park|valencia|
        santa clarita|lancaster|palmdale|
        orange county|irvine|costa mesa|newport beach|huntington beach|santa ana|
        anaheim|orange|tustin|fullerton|brea|yorba linda|garden grove|westminster|
        fountain valley|seal beach|cypress|buena park|lake forest|aliso viejo|
        laguna hills|laguna niguel|laguna beach|mission viejo|rancho santa margarita|
        san clemente|san juan capistrano|dana point|foothill ranch""",
}
# Names that are also somewhere else (Newark NJ, Dublin Ireland, Richmond VA,
# Concord NH, Orange TX, "LA" Louisiana…): they count only when the posting
# also says CA / California.
AMBIGUOUS = {"newark", "dublin", "richmond", "concord", "orange", "carson", "la",
             "venice", "glendale", "walnut", "fairfield", "peninsula", "south bay",
             "east bay", "north bay", "lakewood", "westminster", "cypress", "brentwood",
             "arcadia", "danville", "lafayette", "antioch", "belmont", "city of industry"}


def region_cities(names) -> list[str]:
    out = []
    for n in names:
        key = n.lower().replace(" ", "_")
        if key not in REGIONS:
            raise ValueError(f"unknown location region '{n}' — use one of {sorted(REGIONS)}")
        out += [c.strip() for c in REGIONS[key].replace("\n", "").split("|") if c.strip()]
    return out


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


def location_verdict(location: str, cities: list[str], allow_remote: bool = True) -> str:
    """'in' | 'remote' | 'out' | 'unknown' for one posting's location string.

    'in': some office is in one of `cities`. Multi-office strings ("San
    Francisco, CA; New York, NY") need only one. 'remote': remote and open to
    the US — not remote somewhere else ("Remote - UK") or limited to other
    states ("Remote - New York").
    """
    loc = (location or "").lower().strip()
    if not loc:
        return "unknown"
    # "Santa Clara or San Jose" is two places; "Portland, OR" is Oregon.
    parts = [p.strip() for p in re.split(r";|\||\n|\s/\s|(?<!,)\s+or\s+|\+\s*\d+\s+others?|•", loc)
             if p.strip()]
    saw_place = False
    remote_ok = False
    for part in parts:
        named = _states_named(part)
        says_ca = "ca" in named
        province = re.search(_CA_PROVINCES, part) is not None
        # "Dublin, CA" is the East Bay, not Ireland.
        foreign = province or (_foreign(part) and not says_ca)
        other_state = bool(named - {"ca"})
        if named or foreign:
            saw_place = True
        if not foreign and not other_state:
            for c in cities:
                if _word_re(c).search(part) and (says_ca or c not in AMBIGUOUS):
                    return "in"
        if re.search(_REMOTE, part):
            saw_place = True
            if foreign and not re.search(_US, part):
                continue
            if other_state and not says_ca:
                continue  # "Remote - New York only"
            remote_ok = True
    if remote_ok and allow_remote:
        return "remote"
    return "out" if saw_place or remote_ok else "unknown"


def _location_rule(cfg: dict):
    """Returns f(job) -> bool, or None when the check is off."""
    lc = cfg.get("location") or {}
    regions = lc.get("regions") or []
    if not regions:
        return None
    cities = region_cities(regions) + [c.lower() for c in lc.get("extra_cities") or []]
    allow_remote = lc.get("allow_remote", True)
    unknown = (lc.get("unknown") or "drop").lower()

    def ok(j: Job) -> bool:
        v = location_verdict(j.location, cities, allow_remote)
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
