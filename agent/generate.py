"""Stage 4: build the application packet. Opus running the uploaded
resume-improver Skill in a code-execution container.

The Skill is referenced by ID (RESUME_SKILL_ID in .env), not pasted into the
prompt, so generation runs the actual methodology — scoring rubric,
three-version requirement, manager feedback and metrics — rather than a
paraphrase of it that drifts. `setup_skill.py` uploads it.

The container has no network access. Company research arrives as text from
stage 3; nothing here can look anything up.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import llm

# Skills need the code-execution tool. This is the version the Skills guide
# documents; the container it starts is where the Skill's files are read.
CODE_EXECUTION_TOOL = {"type": "code_execution_20250825", "name": "code_execution"}
CODE_EXECUTION_BETA = "code-execution-2025-08-25"

DELIVERABLES = [
    "resume_recruiter.md",
    "resume_hiring_manager.md",
    "resume_business_outcomes.md",
    "resume_final.md",
    "cover_letter.md",
    "brief.md",
]

BUILD_PROMPT = """Use the resume-improver skill to build an application packet for \
the role below. Follow the skill's methodology in full: its X-Y-Z bullet formula, its \
three versions, its scoring rubric, and its iterate-on-the-weakest-area loop. Use the \
manager feedback and metrics in the skill as source material where they are relevant \
to this role.

HARD RULES — these override anything else, including the skill:
1. Do not invent employers, job titles, dates, degrees, certifications, credentials, \
or metrics. Every employer, title, date range, degree and credential in the output \
must appear in BASE RESUME or in the skill's own source material.
2. Every number in a bullet must come from BASE RESUME or the skill's source material. \
You may reword how a real metric is expressed; you may not create, round up, combine \
or extrapolate one. If a bullet would be stronger with a metric that doesn't exist, \
leave it unquantified and list it under "Metrics to dig up" in brief.md.
3. COMPANY RESEARCH lines marked "UNVERIFIED:" must not be used anywhere in the resume \
or cover letter. Do not repeat them as fact in brief.md either; you may list them as \
things to confirm.
4. Do not claim skills, tools or domains the candidate has no evidence for, even if \
the posting asks for them. Missing requirements go in brief.md, not in the resume.
5. brief.md must name the candidate's genuine weak spots for this role — requirements \
they don't meet or only partly meet, and how a hiring manager is likely to read that. \
Do not soften them into strengths. A brief that finds no weaknesses is wrong.

DELIVERABLES
Write each of these as a markdown file in /mnt/user-data/outputs/ (create the folder), \
and ALSO repeat each one in your final reply between the markers shown, so nothing is \
lost if a file can't be returned:

=== FILE: resume_recruiter.md ===
(skill version 1 — easy to skim, strong metrics)
=== FILE: resume_hiring_manager.md ===
(skill version 2 — more technical / executive)
=== FILE: resume_business_outcomes.md ===
(skill version 3 — business outcomes)
=== FILE: resume_final.md ===
(the version you recommend sending, after iterating on its weakest score)
=== FILE: cover_letter.md ===
(under 300 words, specific to this company and role, no clichés)
=== FILE: brief.md ===
(fit score per the skill's rubric for each version and why; genuine weak spots; \
"Metrics to dig up"; UNVERIFIED research points to confirm; 3 likely interview \
questions with the story from the resume that answers each)
=== END ===

ROLE
title: {title}
company: {company}
location: {location}
url: {url}
screen notes: {screen_reason}

POSTING
{description}

COMPANY RESEARCH (gathered in a separate step; you cannot browse)
{research}

BASE RESUME
{resume}"""


def _skill_ref() -> dict:
    skill_id = os.environ.get("RESUME_SKILL_ID")
    if not skill_id:
        raise RuntimeError("RESUME_SKILL_ID not set — run `python setup_skill.py` first")
    return {"type": "custom", "skill_id": skill_id,
            "version": os.environ.get("RESUME_SKILL_VERSION", "latest")}


def generate(c, model: str, job, research_text: str, resume: str, out_dir: Path,
             screen_reason: str = "", effort: str = "high") -> list[Path]:
    prompt = BUILD_PROMPT.format(
        title=job.title, company=job.company, location=job.location or "unknown",
        url=job.url, screen_reason=screen_reason or "-",
        description=(job.description or "(none)")[:15000],
        research=research_text, resume=resume)
    msg, blocks = llm.run_turn(
        c, "generate", model=model,
        betas=[CODE_EXECUTION_BETA],
        max_tokens=64000,
        output_config={"effort": effort},
        container={"skills": [_skill_ref()]},
        tools=[CODE_EXECUTION_TOOL],
        messages=[{"role": "user", "content": prompt}],
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    written = save_from_text(llm.text_of(blocks), out_dir)
    written += _download_files(c, blocks, out_dir, skip={p.name for p in written})
    if not any(p.name == "brief.md" for p in written):
        raise RuntimeError(f"generation produced no brief.md (stop_reason={msg.stop_reason})")
    return written


_MARKER = re.compile(r"^=== FILE: ([\w.\-]+) ===\s*$", re.M)


def save_from_text(text: str, out_dir: Path) -> list[Path]:
    """Split the reply on `=== FILE: name ===` markers and save each part."""
    written = []
    parts = _MARKER.split(text)
    # parts = [preamble, name1, body1, name2, body2, ...]
    for name, body in zip(parts[1::2], parts[2::2]):
        body = body.split("=== END ===")[0].strip()
        name = os.path.basename(name)
        if not body or name not in DELIVERABLES:
            continue
        p = out_dir / name
        p.write_text(body + "\n")
        written.append(p)
    return written


def _download_files(c, blocks, out_dir: Path, skip: set[str]) -> list[Path]:
    """Files the container produced (e.g. a .docx the skill chose to make)."""
    written = []
    for b in blocks:
        if getattr(b, "type", "") != "bash_code_execution_tool_result":
            continue
        res = b.content
        if getattr(res, "type", "") != "bash_code_execution_result":
            continue
        for ref in getattr(res, "content", None) or []:
            fid = getattr(ref, "file_id", None)
            if not fid:
                continue
            try:
                meta = c.files.retrieve_metadata(fid)
                name = os.path.basename(meta.filename)
                if not name or name in (".", "..") or name in skip:
                    continue
                c.files.download(fid).write_to_file(str(out_dir / name))
                written.append(out_dir / name)
            except Exception as e:  # a lost bonus file shouldn't sink the packet
                print(f"    ! could not download {fid}: {e}")
    return written
