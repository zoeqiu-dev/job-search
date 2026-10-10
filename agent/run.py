"""Run the pipeline.

  python -m agent.run --dry-run     # everything free: collect, filter, rank, rule screen
  python -m agent.run               # full run
  python -m agent.run -v            # also show what the title filter dropped

Four stages, each its own step — not one agent loop:

  1. collect   official APIs + public ATS boards, then hard filters and dedupe  (free)
  2. screen    title prefilter, then rules or Haiku on the description         (free / pennies)
  3. research  Opus + web search, top few only                                 ($)
  4. generate  Opus + the uploaded resume Skill, research passed in as text    ($$)

Cost is controlled in this order: max_packets_per_run, min_score_to_generate,
then the free title prefilter deciding what's worth screening at all.
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import yaml

from . import llm, sources, titles
from .screen import rule_screen, screen
from .state import State

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines) so we don't need python-dotenv.
    Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def candidate_profile(cfg: dict, resume: str) -> str:
    c = cfg.get("candidate") or {}
    q = cfg.get("qualifications") or {}
    return (f"Target titles: {', '.join(c.get('target_titles') or [])}\n"
            f"Based in: {c.get('location', '')}; open to: "
            f"{', '.join((cfg.get('search') or {}).get('locations') or [])}\n"
            f"Years of experience: {q.get('years_experience') or 'see resume'}\n\n"
            f"RESUME\n{resume}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent.run")
    ap.add_argument("--dry-run", action="store_true",
                    help="free stages only; no Anthropic calls, nothing recorded")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--titles", nargs="+", metavar="TITLE",
                    help='search these titles this run instead of config, e.g. '
                         '--titles "Staff Product Manager" "Director of Product"')
    ap.add_argument("--rescreen", action="store_true",
                    help="look again at postings already screened (e.g. after changing "
                         "titles or qualifications); packets already built are still skipped")
    args = ap.parse_args(argv)

    load_env(ROOT / ".env")
    cfg = yaml.safe_load(Path(args.config).read_text())
    if args.titles:
        # The titles become the whole search: config keywords are written for
        # the configured titles and would drag the old search along.
        cfg.setdefault("candidate", {})["target_titles"] = args.titles
        cfg.setdefault("search", {}).update(keywords=[], keywords_from_titles=True)
        print(f"Titles for this run: {', '.join(args.titles)}")
    s = cfg.get("search") or {}
    sc = cfg.get("screen") or {}
    models = cfg.get("models") or {}
    mode = (sc.get("mode") or "rules").lower()

    state = State(ROOT / "state.db")
    if not args.dry_run:
        pruned = state.prune(cfg.get("state_retention_days", 120))
        if pruned:
            print(f"  · pruned {pruned} old postings from state.db")

    # ── 1. collect ────────────────────────────────────────────────────────────
    print("Collecting")
    jobs = sources.collect(cfg)
    if args.rescreen:
        fresh = [j for j in jobs if state.status(j.fingerprint) != "packet"]
    else:
        fresh = [j for j in jobs if not state.seen(j.fingerprint)]
    print(f"  = {len(jobs)} after filters + dedupe, {len(fresh)} "
          f"{'to look at' if args.rescreen else 'not seen before'}")

    # ── 2a. free title prefilter: rank everything, keep the best N ───────────
    ranked, dropped = titles.rank(fresh, cfg, verbose=args.verbose)
    limit = s.get("max_jobs_per_run", 40)
    to_screen = ranked[:limit]
    print(f"Title filter: {len(ranked)} plausible, {len(dropped)} dropped; "
          f"screening the best {len(to_screen)}")

    # ── 2b. description screen ───────────────────────────────────────────────
    results: dict[str, dict] = {}
    blocked: set[str] = set()   # hard fails from the rule screen
    if mode in ("rules", "hybrid"):
        results = rule_screen(to_screen, cfg)
        blocked = {fp for fp, r in results.items() if r["blockers"]}
    if mode in ("haiku", "hybrid"):
        candidates = [j for j in to_screen if j.fingerprint not in blocked]
        if args.dry_run:
            print(f"  (dry run: skipping Haiku on {len(candidates)} postings)")
        elif candidates:
            resume = _read_resume()
            floor = s.get("min_salary") or 0
            target = f"${floor:,}+ base" if floor else "no stated target"
            haiku = screen(llm.client(), models.get("screen", "claude-haiku-5-5"),
                           candidate_profile(cfg, resume), candidates,
                           salary_target=target, effort=sc.get("haiku_effort", "low"))
            for fp, r in haiku.items():
                if fp in results:
                    r["reason"] = f"{r['reason']} | rules: {results[fp]['reason']}"
                results[fp] = r
    if not results and to_screen and not args.dry_run:
        print("  ! screen produced no results")

    list_floor = s.get("min_score_to_list", 60)
    # Rule blockers are hard. Haiku's "blockers" are its notes on gaps; its
    # score already reflects them (e.g. capped at 60 for a pay shortfall).
    matches = sorted(
        (j for j in to_screen if j.fingerprint in results
         and j.fingerprint not in blocked
         and results[j.fingerprint]["score"] >= list_floor),
        key=lambda j: results[j.fingerprint]["score"], reverse=True)

    print(f"\nMatches ({len(matches)}) — title fits and description checks out:")
    for j in matches:
        r = results[j.fingerprint]
        print(f"  {r['score']:>3}  {j.title[:55]:<55}  {j.company[:18]:<18}  {j.url}")
        if args.verbose:
            print(f"       {r['reason']}")
    if args.verbose:
        rejected = [j for j in to_screen if j not in matches and j.fingerprint in results]
        if rejected:
            print("\n  Screened out:")
            for j in rejected:
                r = results[j.fingerprint]
                print(f"    {r['score']:>3}  {j.title[:55]:<55}  {r['reason']}")

    out_root = ROOT / "out" / date.today().isoformat()
    if args.dry_run:
        print("\nDry run: nothing recorded, no packets built.")
        return 0

    out_root.mkdir(parents=True, exist_ok=True)
    _write_matches(out_root / "matches.md", matches, results)
    print(f"\nWrote {out_root / 'matches.md'}")

    match_set = {j.fingerprint for j in matches}
    for j in to_screen:
        if j.fingerprint in results:
            state.record(j, "matched" if j.fingerprint in match_set else "screened",
                         results[j.fingerprint])

    # ── 3 + 4. research and generate, top few only ───────────────────────────
    gen_floor = s.get("min_score_to_generate", 78)
    cap = s.get("max_packets_per_run", 3)
    picks = [j for j in matches
             if results[j.fingerprint]["score"] >= gen_floor and len(j.description) >= 200][:cap]
    if not picks:
        print(f"No posting cleared min_score_to_generate ({gen_floor}); no packets built.")
    else:
        resume = _read_resume()
        c = llm.client()
        print(f"\nBuilding {len(picks)} packet(s) (cap {cap}, floor {gen_floor})")
        # Each packet is research → generate in sequence; packets run side by side.
        with ThreadPoolExecutor(max_workers=min(3, len(picks))) as pool:
            done = list(pool.map(
                lambda j: _packet(c, j, results[j.fingerprint], resume, out_root, models, sc),
                picks))
        for j, (ok, where) in zip(picks, done):
            state.record(j, "packet" if ok else "failed", packet_dir=where if ok else "")
            print(f"  {'✓' if ok else '✗'} {j.title[:50]} @ {j.company}: {where}")

    spent = llm.spend()
    if spent:
        total = sum(spent.values())
        parts = ", ".join(f"{k} ${v:.2f}" for k, v in spent.items())
        print(f"\nEstimated spend this run: ${total:.2f} ({parts})")
    state.close()
    return 0


def _read_resume() -> str:
    p = ROOT / "base_resume.md"
    if not p.exists():
        sys.exit("base_resume.md not found — put your resume (markdown) in the repo root.")
    return p.read_text()


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def _packet(c, job, screen_result, resume, out_root, models, sc) -> tuple[bool, str]:
    from .generate import generate
    from .research import research
    folder = out_root / f"{_slug(job.company)}--{_slug(job.title)}"
    try:
        notes = research(c, models.get("research", "claude-opus-5-5"), job,
                         effort=sc.get("research_effort", "medium"))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "research.md").write_text(notes + "\n")
        (folder / "posting.md").write_text(
            f"# {job.title} — {job.company}\n\n{job.location}\n\n{job.url}\n\n"
            f"Salary: {job.salary or 'not stated'}\n\n{job.description}\n")
        generate(c, models.get("generate", "claude-opus-5-5"), job, notes, resume, folder,
                 screen_reason=screen_result.get("reason", ""),
                 effort=sc.get("generate_effort", "high"))
        return True, str(folder.relative_to(ROOT))
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _write_matches(path: Path, matches: list, results: dict) -> None:
    lines = [f"# Matches — {date.today().isoformat()}", "",
             "| score | title | company | location | link |", "|---|---|---|---|---|"]
    for j in matches:
        r = results[j.fingerprint]
        cells = [str(r["score"]), j.title, j.company, j.location or "", f"[apply]({j.url})"]
        lines.append("| " + " | ".join(c.replace("|", "/") for c in cells) + " |")
    lines += ["", "## Why", ""]
    for j in matches:
        lines.append(f"- **{j.title}** @ {j.company}: {results[j.fingerprint]['reason']}")
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    sys.exit(main())
