#!/usr/bin/env python3
"""
Test every ATS board token in config.yaml and say exactly what each one does.

  python check_boards.py                  # test what's in config.yaml
  python check_boards.py stripe figma     # test specific tokens across all three ATSs
  python check_boards.py --titles         # also print the first few job titles
  python check_boards.py --add figma notion   # test, then add every token that works
                                              # to config.yaml (expands the search)

Companies move between ATS providers and change their board token without
notice, so a token that worked last year may 404 today. This tells you which.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import requests
import yaml

from agent.sources import location_verdict

ROOT = Path(__file__).resolve().parent
UA = {"User-Agent": "personal-job-agent/1.0"}

ENDPOINTS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
                   lambda d: d.get("jobs", []),
                   lambda j: j.get("title", ""),
                   lambda j: (j.get("location") or {}).get("name", "")),
    "lever":      ("https://api.lever.co/v0/postings/{t}?mode=json",
                   lambda d: d if isinstance(d, list) else [],
                   lambda j: j.get("text", ""),
                   lambda j: (j.get("categories") or {}).get("location", "")),
    "ashby":      ("https://api.ashbyhq.com/posting-api/job-board/{t}",
                   lambda d: d.get("jobs", []),
                   lambda j: j.get("title", ""),
                   lambda j: j.get("location", "")),
}

OK, WARN, BAD, DIM = "\033[32m", "\033[33m", "\033[31m", "\033[90m"
END = "\033[0m"


def probe(ats: str, token: str, show_titles: bool = False, state: str = "ca") -> bool:
    url_tpl, extract, title_of, loc_of = ENDPOINTS[ats]
    url = url_tpl.format(t=token)
    label = f"  {ats:<11} {token:<22}"

    try:
        r = requests.get(url, headers=UA, timeout=15)
    except requests.RequestException as e:
        print(f"{label} {BAD}network error{END}  {type(e).__name__}")
        return False

    if r.status_code == 404:
        print(f"{label} {BAD}404{END} no such board — wrong token, or they left {ats}")
        return False
    if r.status_code == 403:
        print(f"{label} {WARN}403{END} blocked — board may be private, or you're rate-limited")
        return False
    if r.status_code != 200:
        print(f"{label} {BAD}HTTP {r.status_code}{END}")
        return False

    try:
        jobs = extract(r.json())
    except ValueError:
        print(f"{label} {BAD}bad response{END} (not JSON — token may be an HTML page)")
        return False

    if not jobs:
        print(f"{label} {WARN}200 but 0 openings{END} — valid board, nothing posted right now")
        return True

    here = sum(location_verdict(loc_of(j), state) in ("in", "remote") for j in jobs)
    print(f"{label} {OK}{len(jobs):>4} openings{END}  {here} in {state.upper()} or remote")
    if show_titles:
        for j in jobs[:4]:
            print(f"{DIM}                                    · {title_of(j)[:62]}{END}")
        if len(jobs) > 4:
            print(f"{DIM}                                    · …and {len(jobs)-4} more{END}")
    return True


def add_to_config(ats: str, token: str, path: Path) -> bool:
    """Append `token` under `ats:` → `<ats>:` in config.yaml, keeping comments.
    Returns False if it was already there."""
    lines = path.read_text().splitlines()
    try:
        top = next(i for i, l in enumerate(lines) if l.rstrip() == "ats:")
        key = next(i for i in range(top + 1, len(lines))
                   if re.match(rf"^  {ats}:\s*(\[\])?\s*(#.*)?$", lines[i]))
    except StopIteration:
        raise SystemExit(f"config.yaml has no ats: → {ats}: block to add to")
    end = key + 1
    items = []
    while end < len(lines) and lines[end].startswith("    "):
        items.append(lines[end].strip().lstrip("- ").strip().strip('"'))
        end += 1
    if token in items:
        return False
    if lines[key].rstrip().endswith("[]"):
        lines[key] = lines[key].replace("[]", "").rstrip()
    lines.insert(end, f"    - {token}")
    path.write_text("\n".join(lines) + "\n")
    return True


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    show_titles = "--titles" in sys.argv

    if "--add" in sys.argv:
        if not args:
            print("Usage: python check_boards.py --add TOKEN [TOKEN ...]")
            return 1
        added = []
        for token in args:
            for ats in ENDPOINTS:
                if probe(ats, token, show_titles) and add_to_config(ats, token, ROOT / "config.yaml"):
                    added.append(f"{ats}:{token}")
        print(f"\nAdded to config.yaml: {', '.join(added) or 'nothing new'}\n")
        return 0

    if args:
        print(f"\nTrying {len(args)} token(s) against all three providers:\n")
        for token in args:
            for ats in ENDPOINTS:
                probe(ats, token, show_titles)
            print()
        return 0

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    ats_cfg = cfg.get("ats") or {}
    total = sum(len(v or []) for v in ats_cfg.values())
    if not total:
        print("\nNo ATS tokens in config.yaml. Add some under the `ats:` block.\n")
        return 1

    print(f"\nTesting {total} board token(s) from config.yaml\n")
    working = 0
    for ats in ENDPOINTS:
        for token in (ats_cfg.get(ats) or []):
            working += probe(ats, token, show_titles)

    print(f"\n{working}/{total} boards responding.")
    if working < total:
        print("\nFor each failure: search \"<company> careers\" and look at the URL of their\n"
              "job board. The token is the last path segment:\n"
              "  job-boards.greenhouse.io/TOKEN   jobs.lever.co/TOKEN   jobs.ashbyhq.com/TOKEN\n"
              "If the URL is none of those three, that company uses a different ATS\n"
              "(Workday, iCIMS, SmartRecruiters) and this agent can't read it directly —\n"
              "you'll pick those roles up through Adzuna or Google Jobs instead.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
