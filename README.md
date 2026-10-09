# job-search

Finds job postings whose **title** fits, checks the **description** against
your qualifications, and lists the URLs that pass. For the best few it also
researches the company and builds a tailored resume packet with your
resume-improver Skill.

```
python -m agent.run --dry-run   # free: collect, filter, rank titles, rule-screen, print URLs
python -m agent.run             # also writes out/<date>/matches.md and builds packets
python -m agent.run -v          # show what the title filter and screen dropped, and why
```

## 1. How it works

Four stages. Each is its own step — not one agent loop.

| stage | what | cost |
|---|---|---|
| 1 collect | Adzuna, Google Jobs (SerpApi), public ATS boards, optional alert emails → hard filters (title terms, companies, salary, ATS location) → dedupe | free |
| 2 screen | **title prefilter** (`agent/titles.py`, string matching) ranks every title and keeps the best `max_jobs_per_run`; then the **description screen** (`screen.mode`) | free (`rules`) / pennies (`haiku`) |
| 3 research | Opus + web search on the top `max_packets_per_run` postings scoring ≥ `min_score_to_generate` | $ |
| 4 generate | Opus runs the uploaded Skill in a code-execution container, with stage 3's notes passed in as text | $$ |

The rule screen (`screen.mode: rules`) is plain if/then logic over the
`qualifications:` block in `config.yaml`:

- a **dealbreaker** phrase anywhere in the description → blocked
- **years** asked for in the required section > yours + `stretch_years` → blocked (within stretch: penalised)
- a **degree** above yours stated as a hard requirement (not "or equivalent", not under "preferred") → blocked
- fewer than `min_skill_hits` of your **skills** named → blocked; more hits → higher score

Every check is skipped while its field is empty, so fill that block in.
Postings with no blocker and a score ≥ `min_score_to_list` are written to
`out/<date>/matches.md` with their URLs.

Research has to happen before generation, in its own call: Skills run in a
code-execution container with **no network access**. Lines research couldn't
confirm are marked `UNVERIFIED:` and generation is told not to use them. The
anti-fabrication rules are in `BUILD_PROMPT` in `agent/generate.py`.

## 2. Why no LinkedIn or Indeed scraping

Indeed has no public job-search API (the Publisher API was retired) and blocks
automated traffic. LinkedIn's jobs API is enterprise-only, and scraping puts
your personal account at risk. Postings from both are reached through Google
Jobs via SerpApi, and optionally by parsing job-alert emails sent to your own
inbox over IMAP (`sources.linkedin_alert_email`).

## 3. Setup

```
pip install -r requirements.txt
cp .env.example .env                      # add ANTHROPIC_API_KEY, optional source keys
cp ~/path/to/resume.md base_resume.md     # gitignored
cp -r ~/path/to/resume-improver skill_bundle/   # gitignored
python setup_skill.py                     # uploads the skill, writes RESUME_SKILL_ID to .env
python check_boards.py                    # confirm every ATS token responds
```

Edit `config.yaml` directly or open `configurator.html` in a browser and copy
its output over it.

Re-run `python setup_skill.py` after editing the skill: it uploads a new
version (a full snapshot, not a delta). Generation uses `latest` unless
`RESUME_SKILL_VERSION` pins one.

## 4. Cost

In order of how much they matter: `max_packets_per_run`, then
`min_score_to_generate`, then the free title prefilter. Each run prints an
estimated spend. Don't move screening onto a large model: it's the stage that
sees every posting.

## 5. Tests

```
python -m pytest -q
```

Covers the title filter (real roles vs look-alikes), salary parsing and
filtering, company dedupe, location filtering, the rule screen, and the full
pipeline against a fake Anthropic client (request shapes only — no model
output is tested).

## 6. Files

```
agent/sources.py    collection, salary parsing, filters, dedupe
agent/titles.py     free title scoring and ranking
agent/screen.py     rule screen + Haiku screen
agent/research.py   stage 3 (web search)
agent/generate.py   stage 4 (Skill) — BUILD_PROMPT lives here
agent/llm.py        client, retries, pause_turn, refusal fallback, spend tally
agent/state.py      state.db: what's been seen, so nothing is paid for twice
agent/run.py        the pipeline
setup_skill.py      upload the skill
check_boards.py     test ATS tokens
configurator.html   config editor
```
