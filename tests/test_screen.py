from agent.screen import (degree_required, rule_screen_one, split_required,
                          years_required)
from agent.sources import Job

BASE = ("About the role. You will own the roadmap for our ML platform, write PRDs, run "
        "customer interviews and partner cross-functional teams on developer tools and "
        "infrastructure. " * 3)

CFG = {"qualifications": {
    "years_experience": 6, "stretch_years": 2, "degree": "bachelor",
    "skills": ["ml platform", "roadmap", "prd", "customer interviews", "developer tools",
               "infrastructure", "kubernetes"],
    "skills_for_full_marks": 5, "min_skill_hits": 1,
    "dealbreakers": ["security clearance", "ts/sci"],
}}


def _job(desc, title_score=80):
    j = Job("Senior Product Manager", "Acme", url="u", description=desc)
    j.extra["title_score"] = title_score
    return j


def test_years_required():
    assert years_required("5+ years of product management experience") == 5
    assert years_required("5-8 years experience in PM; 2+ years with ML") == 5
    assert years_required("Minimum of 7 years of relevant experience") == 7
    assert years_required("Experience: 4 or more years shipping software") == 4
    assert years_required("We were founded 10 years ago.") is None


def test_preferred_section_not_required():
    desc = ("Requirements: 5+ years of PM experience.\nPreferred qualifications: "
            "10+ years of experience. PhD in machine learning.")
    req, pref = split_required(desc)
    assert years_required(req) == 5
    assert degree_required(req) == 0
    assert "PhD" in pref


def test_degree_softeners():
    assert degree_required("PhD in Computer Science required.") == 3
    assert degree_required("BS/BA in Computer Science or equivalent experience.") == 0
    assert degree_required("Master's degree preferred.") == 0
    assert degree_required("Bachelor's degree in a technical field.") == 1


def test_good_match_scores_high():
    r = rule_screen_one(_job(BASE + "Requirements: 5+ years of product management experience."), CFG)
    assert r["blockers"] == [] and r["score"] >= 78, r


def test_dealbreaker_blocks():
    r = rule_screen_one(_job(BASE + "Must hold an active TS/SCI security clearance."), CFG)
    assert r["blockers"] and r["score"] <= 30


def test_too_many_years_blocks_stretch_penalised():
    blocked = rule_screen_one(_job(BASE + "Requires 12+ years of product experience."), CFG)
    assert any("12+" in b for b in blocked["blockers"])
    stretch = rule_screen_one(_job(BASE + "Requires 8+ years of product experience."), CFG)
    ok = rule_screen_one(_job(BASE + "Requires 5+ years of product experience."), CFG)
    assert stretch["blockers"] == [] and stretch["score"] < ok["score"]


def test_degree_above_held_blocks():
    r = rule_screen_one(_job(BASE + "A PhD in machine learning is required."), CFG)
    assert any("PhD" in b for b in r["blockers"])


def test_unset_qualifications_skip_checks():
    r = rule_screen_one(_job(BASE + "PhD required. 15+ years of experience."), {"qualifications": {}})
    assert r["blockers"] == []


def test_no_skill_overlap_blocks():
    r = rule_screen_one(_job("We sell shoes. " * 30), CFG)
    assert r["blockers"]


def test_missing_description_capped_not_blocked():
    r = rule_screen_one(_job(""), CFG)
    assert r["blockers"] == [] and r["score"] <= 60
