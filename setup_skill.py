#!/usr/bin/env python3
"""
Upload skill_bundle/resume-improver/ to the Skills API.

  python setup_skill.py            # first run creates the skill; later runs add a version

First run writes RESUME_SKILL_ID to .env; generate.py references the skill by
that ID. Every later run uploads a new version. Versions are full snapshots,
not deltas: the whole folder goes up each time, and the `name` in SKILL.md
must stay the same. generate.py uses "latest" unless RESUME_SKILL_VERSION pins one.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import anthropic
from anthropic.lib import files_from_dir

from agent.run import load_env

ROOT = Path(__file__).resolve().parent
BUNDLE = ROOT / "skill_bundle" / "resume-improver"
ENV = ROOT / ".env"


def set_env(key: str, value: str) -> None:
    text = ENV.read_text() if ENV.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, re.M):
        text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    ENV.write_text(text)


def main() -> int:
    if not (BUNDLE / "SKILL.md").exists():
        print(f"No SKILL.md in {BUNDLE.relative_to(ROOT)}/ — copy your resume-improver "
              "skill folder there first.")
        return 1
    load_env(ENV)
    import os
    client = anthropic.Anthropic()
    files = files_from_dir(BUNDLE)
    skill_id = os.environ.get("RESUME_SKILL_ID")

    if skill_id:
        v = client.skills.versions.create(skill_id=skill_id, files=files)
        print(f"Uploaded new version of {skill_id}: {v.id}")
    else:
        skill = client.skills.create(files=files)
        set_env("RESUME_SKILL_ID", skill.id)
        print(f"Created skill {skill.id} (version {skill.latest_version_id}); saved to .env")
    print(f"{len(files)} file(s) from {BUNDLE.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
