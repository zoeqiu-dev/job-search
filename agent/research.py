"""Stage 3: company + role research. Opus with web search.

This has to be its own call, before generation. Skills run in a
code-execution container with no network access, so the generation call
cannot look anything up; whatever it knows about the company arrives as the
text this stage returns.

Anything the model could not confirm from a source it read is marked
UNVERIFIED, and generation is told not to use those lines.
"""
from __future__ import annotations

from . import llm

RESEARCH_PROMPT = """You are researching a job opening so a candidate can tailor their \
application. Use web search. Prefer the company's own site, its engineering/product \
blog, press releases, earnings or funding announcements, and reputable news from the \
last 18 months.

ROLE
title: {title}
company: {company}
location: {location}
url: {url}

POSTING
{description}

Write markdown with exactly these sections:

## Company
What they sell, to whom, business model, size/stage, recent funding or financials.

## Product area for this role
Which product, team, or platform this role most likely sits in, and what that \
product does. Name the product if the posting or a source does.

## Recent developments
Launches, strategy shifts, leadership changes, layoffs, acquisitions — dated.

## What they will care about
The 4-6 things a hiring manager for this role will screen hardest on, inferred from \
the posting and the research. One line each, with why.

## Language to mirror
Specific terms, product names and values the company uses publicly, which an \
application can echo honestly.

## Risks and open questions
Anything a candidate should know before applying or ask in an interview.

## Sources
Bulleted URLs you actually read.

RULES
- Every factual claim must come from a source you read in this session. If you \
believe something but could not confirm it, keep it and prefix the line with \
"UNVERIFIED:". Never present an unconfirmed claim as fact.
- Do not guess team names, headcounts, revenue, or the hiring manager's identity.
- If the posting and the web disagree, say so.
- No preamble, no closing summary. Just the sections."""


def research(c, model: str, job, effort: str = "medium", max_searches: int = 6) -> str:
    prompt = RESEARCH_PROMPT.format(
        title=job.title, company=job.company, location=job.location or "unknown",
        url=job.url, description=(job.description or "(none)")[:12000])
    msg, blocks = llm.run_turn(
        c, "research", model=model,
        max_tokens=16000,
        output_config={"effort": effort},
        tools=[{"type": "web_search_20260209", "name": "web_search",
                "max_uses": max_searches}],
        messages=[{"role": "user", "content": prompt}],
    )
    text = llm.text_of(blocks).strip()
    if not text:
        raise RuntimeError(f"research returned no text (stop_reason={msg.stop_reason})")
    return text
