"""Free title prefilter. String matching only — no model call.

ATS boards return every opening a company has (five boards gave ~2,000), so
truncating to the first N that arrive feeds the screener "Data Center
Architect" while real PM roles are never looked at. Instead every title is
scored against candidate.target_titles and the best N are handed on.

Substring matching isn't enough: "Product Marketing Manager", "Technical
Program Manager", "Engineering Manager, Product Platform" and "Product Owner"
all share words with "Product Manager" and are different jobs. So a title is
first reduced to a *function* (what the job is) and a *level* (how senior),
and only titles whose function matches a target's function score at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── Normalisation ─────────────────────────────────────────────────────────────
# Expanded before anything else so "Sr. PM, ML Infra" reads as
# "senior product manager machine learning infrastructure".
ABBREVIATIONS = {
    "sr": "senior", "snr": "senior", "jr": "junior",
    "pm": "product manager", "spm": "senior product manager",
    "gpm": "group product manager", "apm": "associate product manager",
    "tpm": "technical program manager", "pgm": "program manager",
    "pmm": "product marketing manager", "em": "engineering manager",
    "ml": "machine learning", "genai": "generative ai", "gen ai": "generative ai",
    "infra": "infrastructure", "dx": "developer experience",
    "devex": "developer experience", "mgr": "manager", "mgmt": "management",
    "eng": "engineering", "dir": "director", "prin": "principal",
    "vp": "vice president", "svp": "senior vice president",
    "evp": "senior vice president", "cpo": "chief product officer",
}

_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5"}


def expand(text: str) -> str:
    """Lowercase, strip punctuation, expand abbreviations."""
    t = (text or "").lower().replace("&", " and ")
    t = re.sub(r"[^a-z0-9+#]+", " ", t)
    words = t.split()
    out = []
    i = 0
    while i < len(words):
        two = " ".join(words[i:i + 2])
        if two in ABBREVIATIONS:
            out.append(ABBREVIATIONS[two])
            i += 2
            continue
        out.append(ABBREVIATIONS.get(words[i], words[i]))
        i += 1
    return " ".join(out)


def split_title(title: str) -> tuple[str, str]:
    """("Product Manager, AI Platform") -> ("product manager", "ai platform").

    The head names the job; the qualifier names the team or domain. Judging
    function on the head is what keeps "Engineering Manager, Product Platform"
    out: its head is "engineering manager".
    """
    parts = re.split(r",|\s[-–—|:]\s|\(|\)|\s/\s", title or "", maxsplit=1)
    head = expand(parts[0])
    qual = expand(parts[1]) if len(parts) > 1 else ""
    return head, qual


# ── Function ──────────────────────────────────────────────────────────────────
# Phrases that name a job function. Order matters only for readability; the
# longest phrase found wins (see _function_of).
FUNCTIONS: dict[str, list[str]] = {
    "product_management": [
        "product manager", "product management", "product lead",
        "product leader", "head of product", "director of product",
        "vice president of product", "vice president product",
        "chief product officer", "group product manager",
    ],
}

# Titles that share words with a target function but are a different job.
# Each maps to its own function key, so it can never match "product_management".
CONFUSABLE: dict[str, list[str]] = {
    "product_marketing": ["product marketing", "marketing"],
    "program_management": ["program manager", "program management", "programme manager"],
    "project_management": ["project manager", "project management"],
    "engineering": ["engineering manager", "software engineer", "engineer",
                    "engineering", "developer", "architect", "sre"],
    "product_owner": ["product owner", "scrum master"],
    "design": ["product designer", "product design", "designer", "ux", "ui"],
    "analytics": ["product analyst", "analyst", "data scientist", "scientist",
                  "researcher", "research"],
    "operations": ["product operations", "product ops", "operations"],
    "sales": ["sales", "account manager", "account executive", "account director",
              "customer success", "partner manager", "partnerships",
              "business development", "solutions consultant", "solutions engineer"],
    "support": ["product specialist", "product support", "support", "specialist"],
    "legal": ["product counsel", "counsel", "attorney", "paralegal"],
    "recruiting": ["recruiter", "recruiting", "talent"],
    "writing": ["technical writer", "content designer", "writer"],
    "advocacy": ["developer advocate", "developer relations", "evangelist"],
}

_ALL_PHRASES: list[tuple[str, str]] = sorted(
    [(p, fn) for fn, ps in FUNCTIONS.items() for p in ps]
    + [(p, fn) for fn, ps in CONFUSABLE.items() for p in ps],
    key=lambda x: -len(x[0]),
)


def _find_phrase(text: str, phrase: str) -> int:
    m = re.search(rf"\b{re.escape(phrase)}\b", text)
    return m.start() if m else -1


def _function_of(text: str) -> str | None:
    """Longest function phrase in `text`, so "product marketing manager" is
    product_marketing even though it contains "manager"."""
    hits = []
    for phrase, fn in _ALL_PHRASES:
        if _find_phrase(text, phrase) >= 0:
            hits.append((len(phrase), fn))
    if not hits:
        return None
    # A target-function phrase beats a shorter confusable inside the same text
    # ("technical product manager" has "product manager", not "engineer").
    hits.sort(reverse=True)
    return hits[0][1]


# ── Level ─────────────────────────────────────────────────────────────────────
LEVEL_WORDS = [  # checked longest-first
    ("senior vice president", 9), ("vice president", 8), ("chief", 10),
    ("senior director", 7), ("director", 6), ("head of", 6),
    ("senior principal", 6), ("principal", 5), ("group", 5),
    ("senior staff", 5), ("staff", 4), ("lead", 4),
    ("senior", 3), ("associate", 1), ("junior", 1), ("entry level", 1),
    ("new grad", 1), ("intern", 0), ("internship", 0),
]
MID = 2  # a bare "Product Manager"


def level_of(head: str, qual: str = "", function_from_qualifier: bool = False) -> int:
    text = f"{head} {qual}"
    for word, lvl in LEVEL_WORDS:
        if _find_phrase(head, word) >= 0:
            return lvl
    # "Product Manager II", "Product Manager 3"
    m = re.search(r"\b(i{1,3}|iv|v|[1-5])\b\s*$", head)
    if m:
        n = int(_ROMAN.get(m.group(1), m.group(1)))
        return {1: 1, 2: 2, 3: 3, 4: 4, 5: 5}.get(n, MID)
    # "Manager, Product Management" manages PMs.
    if function_from_qualifier and _find_phrase(head, "manager") >= 0:
        return 5
    if "head of product" in text or "director of product" in text:
        return 6
    return MID


# ── Parsed title ──────────────────────────────────────────────────────────────
@dataclass
class Parsed:
    raw: str
    head: str
    qual: str
    function: str | None
    level: int
    words: set[str]


_STOP = {"of", "and", "the", "for", "a", "an", "in", "to", "on", "with", "at",
         "us", "remote", "hybrid", "onsite"}
_ROLE_WORDS = {"product", "manager", "management", "senior", "principal",
               "group", "staff", "lead", "director", "head", "associate",
               "junior", "chief", "officer", "vice", "president", "leader"}


def parse(title: str) -> Parsed:
    head, qual = split_title(title)
    fn = _function_of(head)
    from_qual = False
    if fn is None and qual:
        fn = _function_of(qual)
        # "Director, Product" / "Senior Manager, Product - Payments": a generic
        # leadership head whose qualifier is just the product org.
        if fn is None and re.match(r"product\b", qual) and re.search(
                r"\b(manager|director|head|lead|vice president)\b", head):
            fn = "product_management"
        from_qual = fn is not None
    lvl = level_of(head, qual, from_qual)
    words = set(f"{head} {qual}".split()) - _STOP
    return Parsed(title, head, qual, fn, lvl, words)


def _domain_words(p: Parsed) -> set[str]:
    """What the role is *about*, minus the words that just name the role."""
    return {w for w in p.words if w not in _ROLE_WORDS and not w.isdigit()}


# ── Scoring ───────────────────────────────────────────────────────────────────
STEP_DOWN = 20   # per level below the target: a step down is a real problem
STEP_UP = 8      # per level above: a stretch, worth a look


def score_title(title: str, targets: list[str], boost_terms: list[str] | None = None) -> tuple[int, str]:
    """0-100 and a short reason. 0 means wrong function."""
    p = parse(title)
    if p.function is None:
        return 0, "no recognisable job function"

    best, why = 0, ""
    for t in targets:
        tp = parse(t)
        if tp.function != p.function:
            continue
        s = 60
        diff = p.level - tp.level
        s -= STEP_DOWN * -diff if diff < 0 else STEP_UP * diff

        t_dom = _domain_words(tp)
        if t_dom:
            # A specific target ("Product Manager, AI") rewards a title in that domain.
            overlap = len(t_dom & _domain_words(p)) / len(t_dom)
            s += round(30 * overlap)
        else:
            s += 20  # a generic target ("Senior Product Manager") is domain-neutral
        if s > best:
            best, why = s, f"≈ {t} (level {p.level} vs {tp.level})"

    if best == 0:
        return 0, f"function is {p.function}, not a target"

    if boost_terms:
        text = f" {p.head} {p.qual} "
        hits = [b for b in boost_terms if f" {expand(b)} " in text]
        if hits:
            best += min(10, 5 * len(hits))
            why += f" +{', '.join(hits)}"
    return max(0, min(100, best)), why


def rank(jobs: list, cfg: dict, verbose: bool = False) -> tuple[list, list]:
    """Score every title, return (kept_best_first, dropped).

    `kept` is everything above search.title_match_floor, sorted by score; the
    caller takes the top search.max_jobs_per_run. Scores are stored on
    job.extra["title_score"] for the screen stage.
    """
    targets = (cfg.get("candidate") or {}).get("target_titles") or []
    s = cfg.get("search") or {}
    floor = s.get("title_match_floor", 45)
    boosts = (cfg.get("filters") or {}).get("boost_terms") or []

    kept, dropped = [], []
    for j in jobs:
        score, why = score_title(j.title, targets, boosts)
        j.extra["title_score"] = score
        j.extra["title_reason"] = why
        (kept if score >= floor else dropped).append(j)

    kept.sort(key=lambda j: j.extra["title_score"], reverse=True)
    if verbose:
        for j in sorted(dropped, key=lambda j: -j.extra["title_score"])[:40]:
            print(f"    - {j.extra['title_score']:>3}  {j.title[:60]}  ({j.extra['title_reason']})")
    return kept, dropped
