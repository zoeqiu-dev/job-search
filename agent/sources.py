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


# ── Adzuna ────────────────────────────────────────────────────────────────────
def from_adzuna(cfg: dict) -> list[Job]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        print("  - Adzuna: no credentials, skipping")
        return []

    s, out = cfg["search"], []
    for kw in s["keywords"]:
        for loc in s["locations"]:
            url = f"https://api.adzuna.com/v1/api/jobs/{s['country']}/search/1"
            r = _get(url, params={
                "app_id": app_id, "app_key": app_key,
                "what": kw,
                "where": "" if loc.lower() == "remote" else loc,
                "results_per_page": 25,
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
                    location=item.get("location", {}).get("display_name", ""),
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


# ── Google Jobs (SerpApi) ─────────────────────────────────────────────────────
def from_google_jobs(cfg: dict) -> list[Job]:
    """Google for Jobs indexes LinkedIn, Indeed, Glassdoor and company boards.
    This is the compliant way to see those postings."""
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        print("  - Google Jobs: no SERPAPI_KEY, skipping")
        return []

    s, out = cfg["search"], []
    for kw in s["keywords"]:
        for loc in s["locations"]:
            r = _get("https://serpapi.com/search", params={
                "engine": "google_jobs", "q": kw,
                "location": None if loc.lower() == "remote" else loc,
                "ltype": "1" if loc.lower() == "remote" else None,
                "hl": "en", "api_key": key,
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
    near = _location_terms(cfg)

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
        if near and not _location_ok(j, near, s.get("country", "us")):
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
        print(f"  · dropped {dropped_loc} ATS postings outside your locations")
    return kept


def _word_re(term: str) -> re.Pattern:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])")


# Remote postings that are remote *somewhere else*.
_FOREIGN = ["uk", "united kingdom", "london", "emea", "europe", "eu", "canada",
            "toronto", "india", "bangalore", "apac", "germany", "berlin", "ireland",
            "dublin", "australia", "sydney", "singapore", "japan", "tokyo", "brazil",
            "mexico", "latam", "france", "paris", "netherlands", "amsterdam",
            "spain", "poland", "israel", "korea", "seoul", "switzerland", "zurich"]


def _location_terms(cfg: dict) -> list[str]:
    """Lowercased place names a posting's location must mention. Empty list
    disables the check."""
    f = cfg.get("filters", {}) or {}
    if not f.get("ats_location_filter", True):
        return []
    s = cfg.get("search", {}) or {}
    terms = []
    for loc in s.get("locations") or []:
        city = loc.split(",")[0].strip().lower()
        if city:
            terms.append(city)
    terms += [t.lower() for t in f.get("nearby_locations") or []]
    return terms


def _location_ok(j: Job, terms: list[str], country: str) -> bool:
    """Applied to ATS boards only. Adzuna and Google Jobs were already queried
    per location, and their "Santa Monica, CA" for a "Los Angeles" search is a
    match this string check would wrongly drop. ATS boards return every office
    worldwide, so without this a London role reaches the screener."""
    if not j.source.split(":")[0] in ("greenhouse", "lever", "ashby"):
        return True
    loc = (j.location or "").lower()
    if not loc:
        return True
    for t in terms:
        if t == "remote":
            if "remote" in loc and not (country == "us" and any(
                    _word_re(x).search(loc) for x in _FOREIGN)):
                return True
            if country == "us" and "anywhere" in loc:
                return True
        elif t in loc:
            return True
    # "San Francisco, CA | New York, NY | Remote (US)": any one acceptable office is enough
    return False


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
