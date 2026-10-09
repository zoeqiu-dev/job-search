"""Stage 2: relevance screen.

Two ways to do it, chosen by screen.mode in config.yaml:

  rules   Free. Plain if/then checks on the description against the
          `qualifications:` block: dealbreaker phrases, years of experience
          asked for, degree asked for, and how many of your skills it names.
  haiku   Haiku reads each posting and scores fit. Pennies per run.
  hybrid  rules first; only postings with no hard blocker go to Haiku.

Only jobs clearing min_score_to_generate go on to the expensive research +
generation stages. Never point this stage at a large model: it sees every
posting that survives the title filter, and that's where cost scales.
"""
from __future__ import annotations

import json
import re

import anthropic

from . import llm

SCREEN_PROMPT = """You are screening job postings for a specific candidate. For each posting, \
score 0-100 on how well it fits, and give a one-line reason.

CANDIDATE PROFILE
{profile}

Scoring guidance:
- 85-100: title, seniority, and domain all align; candidate would be a strong applicant
- 70-84:  good domain fit but some stretch on seniority, title, or a key requirement
- 40-69:  adjacent, would require significant repositioning
- 0-39:   wrong level, wrong function, or a hard blocker (visa, clearance, onsite in wrong city)

Penalise hard: wrong seniority (IC vs manager, junior vs senior), a location that \
conflicts with the candidate's constraints, and postings that are clearly staffing-agency \
reposts rather than a real req.

On pay: the candidate is targeting {salary_target}. If a posting states a band whose top \
falls short of that, cap its score at 60 and list the gap as a blocker. If the posting \
states no pay, do not penalise it — most don't — but infer from seniority and company \
whether the band is plausibly in range, and say so in your reason.

Return ONLY a JSON array, no prose, no markdown fence:
[{{"id": "<the id given>", "score": <int>, "reason": "<max 20 words>", "blockers": ["..."]}}]

POSTINGS
{postings}"""


def screen(client: anthropic.Anthropic, model: str, profile: str, jobs: list,
           batch_size: int = 10, salary_target: str = "no stated target",
           effort: str = "low") -> dict:
    """Returns {fingerprint: {"score": int, "reason": str, "blockers": [...]}}"""
    results: dict[str, dict] = {}

    for i in range(0, len(jobs), batch_size):
        batch = jobs[i:i + batch_size]
        postings = "\n\n".join(
            f"id: {j.fingerprint}\n"
            f"title: {j.title}\n"
            f"company: {j.company}\n"
            f"location: {j.location}\n"
            f"salary: {j.salary or 'not stated'}\n"
            f"description: {(j.description or '(none available)')[:2500]}"
            for j in batch
        )
        msg = client.messages.create(
            model=model,
            # Room for adaptive thinking plus ~10 JSON rows; effort keeps it short.
            max_tokens=8000,
            # Haiku 4.5 rejects `effort`; Haiku 5.5 takes it.
            **({} if "haiku-4" in model else {"output_config": {"effort": effort}}),
            messages=[{
                "role": "user",
                "content": SCREEN_PROMPT.format(
                    profile=profile[:6000], postings=postings,
                    salary_target=salary_target),
            }],
        )
        llm.record("screen", model, msg.usage)
        text = "".join(b.text for b in msg.content if b.type == "text")
        for row in _parse_json_array(text):
            if row.get("id"):
                results[row["id"]] = {
                    "score": int(row.get("score", 0)),
                    "reason": row.get("reason", ""),
                    "blockers": row.get("blockers", []),
                }
    return results


def _parse_json_array(text: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    print("  ! screen: could not parse model output")
    return []


# ── Rule-based screen (free) ──────────────────────────────────────────────────
# Everything below is plain string logic. It reads the description the same
# way you would skim it: is there anything that rules me out, do they want
# more years than I have, do they want a degree I don't have, and does it talk
# about the things I've actually done.

DEGREE_LEVEL = {"none": 0, "bachelor": 1, "master": 2, "mba": 2, "phd": 3}
_DEGREE_PATTERNS = [
    (3, r"\b(ph\.?\s?d|doctorate|doctoral)\b"),
    (2, r"\b(master'?s|m\.s\.|ms degree|m\.sc|mba)\b"),
    (1, r"\b(bachelor'?s|b\.s\.|b\.a\.|bs/ba|ba/bs|undergraduate degree|4.year degree)\b"),
]
# Wording that turns a stated requirement into a soft one.
_SOFTENERS = r"(or equivalent|or related|equivalent (practical |work )?experience|preferred|a plus|nice to have|bonus|ideally)"
# Headings after which the text is "nice to have", not "must have".
_PREFERRED_HEADINGS = re.compile(
    r"(preferred qualifications|preferred skills|preferred experience|nice[- ]to[- ]haves?|"
    r"bonus points|you might also have|it'?s a plus if|ideal candidates? (will )?also|"
    r"what would make you stand out|extra credit)", re.I)
_YEARS = re.compile(
    r"(?:at least |minimum (?:of )?)?(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?"
    r"(?:or more\s+)?(?:years?|yrs?)\b", re.I)


def split_required(description: str) -> tuple[str, str]:
    """(required_part, preferred_part). Text after a 'preferred / nice to
    have' heading is preferred; everything before is treated as required."""
    m = _PREFERRED_HEADINGS.search(description or "")
    if not m:
        return description or "", ""
    return description[:m.start()], description[m.start():]


def years_required(text: str) -> int | None:
    """Highest 'N+ years ... experience' ask in `text`. For a range ("5-8
    years") the low end is the requirement. Numbers not tied to experience
    ("founded 10 years ago") are ignored."""
    asks = []
    for m in _YEARS.finditer(text or ""):
        n = int(m.group(1))
        if not 1 <= n <= 25:
            continue
        window = text[m.end():m.end() + 90].lower()
        before = text[max(0, m.start() - 40):m.start()].lower()
        if "experience" in window or "experience" in before:
            asks.append(n)
    return max(asks) if asks else None


def degree_required(text: str) -> int:
    """Highest degree level stated as a hard requirement in `text` (0 = none).
    A mention softened by 'or equivalent' / 'preferred' in the same sentence
    doesn't count."""
    best = 0
    for sentence in re.split(r"(?<=[.;\n•])\s+|\n", text or ""):
        low = sentence.lower()
        if re.search(_SOFTENERS, low):
            continue
        for lvl, pat in _DEGREE_PATTERNS:
            if re.search(pat, low):
                best = max(best, lvl)
                break
    return best


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])", text) is not None


def rule_screen_one(job, cfg: dict) -> dict:
    """Score one job 0-100 from its title score and its description.
    Same shape as the Haiku screen: {"score", "reason", "blockers"}."""
    q = cfg.get("qualifications") or {}
    title_score = int(job.extra.get("title_score", 0))
    desc = job.description or ""

    if len(desc) < 200:
        # Alert-email stubs and the like. Nothing to check, so it can be
        # listed but is capped below any sane generate threshold.
        return {"score": min(title_score, 60),
                "reason": "no description to check — title match only",
                "blockers": []}

    low = desc.lower()
    required, _preferred = split_required(desc)
    blockers: list[str] = []
    notes: list[str] = []
    penalty = 0

    # 1. Anything that rules you out, wherever it appears.
    for phrase in q.get("dealbreakers") or []:
        if _has(low, phrase):
            blockers.append(f"mentions '{phrase}'")

    # 2. Years of experience asked for in the required section.
    have = q.get("years_experience") or 0
    stretch = q.get("stretch_years", 2)
    asked = years_required(required)
    if have and asked is not None:
        if asked > have + stretch:
            blockers.append(f"asks {asked}+ yrs, you have {have}")
        elif asked > have:
            penalty += 10
            notes.append(f"asks {asked}+ yrs (stretch)")
        else:
            notes.append(f"asks {asked}+ yrs ✓")

    # 3. Degree stated as a hard requirement. Skipped until you set `degree`.
    held = DEGREE_LEVEL.get(str(q.get("degree") or "").lower())
    need = degree_required(required) if held is not None else 0
    if held is not None and need > held:
        name = {1: "bachelor's", 2: "master's", 3: "PhD"}[need]
        blockers.append(f"requires a {name}")

    # 4. How much of the description is about things you've done.
    skills = q.get("skills") or []
    hits = [s for s in skills if _has(low, s)]
    full_marks = max(1, q.get("skills_for_full_marks", 5))
    min_hits = q.get("min_skill_hits", 0)
    if skills and len(hits) < min_hits:
        blockers.append(f"only {len(hits)}/{min_hits} of your skills mentioned")
    if skills:
        notes.append(f"{len(hits)} skill hits" + (f": {', '.join(hits[:4])}" if hits else ""))

    desc_score = (50 + 50 * min(1.0, len(hits) / full_marks)) if skills else 75
    score = round(0.5 * title_score + 0.5 * desc_score) - penalty
    if blockers:
        score = min(score, 30)
    reason = "; ".join(blockers + notes) or "no issues found"
    return {"score": max(0, min(100, score)), "reason": reason[:160], "blockers": blockers}


def rule_screen(jobs: list, cfg: dict) -> dict:
    """{fingerprint: {"score", "reason", "blockers"}} — no API calls."""
    return {j.fingerprint: rule_screen_one(j, cfg) for j in jobs}
