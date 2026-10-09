import pytest

from agent.sources import (Job, _clears_salary, _lever_description, _norm_company,
                           apply_filters, dedupe, parse_salary)


@pytest.mark.parametrize("text, expected", [
    ("Posted 2024. 5 years experience. $190,000-$240,000", (190000, 240000)),
    ("$150K–$200K a year", (150000, 200000)),
    ("$190K – $240K • Offers Equity", (190000, 240000)),
    ("$75 an hour", (156000, 156000)),
    ("$75/hr", (156000, 156000)),
    ("$40-$50 per hour", (83200, 104000)),
    ("$600 per day", (156000, 156000)),
    ("$3,000 per week", (156000, 156000)),
    ("$12,500 per month", (150000, 150000)),
    ("$1.2M", (1200000, 1200000)),
    ("Up to $220K", (None, 220000)),
    ("from $150k", (150000, None)),
    ("", (None, None)),
    ("Competitive", (None, None)),
    # regressions: words that look like a pay period but aren't
    ("$150,000 - $200,000. Reports to HR leadership", (150000, 200000)),
    ("$160k-$200k, 5 days a week in office", (160000, 200000)),
    ("$150k base + monthly bonus", (150000, 150000)),
])
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


def _job(title="Senior Product Manager", company="Acme", **kw):
    kw.setdefault("url", "https://example.com/1")
    return Job(title=title, company=company, **kw)


def test_salary_judged_on_top_of_band():
    j = _job(salary="$120,000 - $200,000")
    assert _clears_salary(j, 180000, False) == (True, True)
    j = _job(salary="$120,000 - $160,000")
    assert _clears_salary(j, 180000, False) == (False, True)


def test_unstated_salary_is_unknown_and_kept():
    assert _clears_salary(_job(), 180000, False) == (True, False)
    cfg = {"search": {"min_salary": 180000}}
    assert len(apply_filters([_job()], cfg)) == 1
    cfg = {"search": {"min_salary": 180000, "salary_unknown": "drop"}}
    assert apply_filters([_job()], cfg) == []


def test_estimated_salary_treated_as_unknown():
    j = _job(salary="$90,000 (estimated)", salary_min=90000, salary_max=90000,
             salary_estimated=True)
    assert _clears_salary(j, 180000, False) == (True, False)
    assert _clears_salary(j, 180000, True) == (False, True)


@pytest.mark.parametrize("name", ["Acme", "ACME Inc", "Acme, LLC", "Acme Inc.", "The Acme Corp"])
def test_company_suffixes_collapse(name):
    assert _norm_company(name) == "acme"


def test_dedupe_across_sources_keeps_longest_description():
    a = _job(company="Acme", location="Remote", description="short", source="adzuna")
    b = _job(company="ACME, Inc.", location="Remote", description="much longer text", source="greenhouse:acme")
    c = _job(company="Acme LLC", location="Remote", description="", source="google_jobs")
    out = dedupe([a, b, c])
    assert len(out) == 1 and out[0].source == "greenhouse:acme"


def test_exclude_terms_match_whole_words_only():
    cfg = {"filters": {"exclude_title_terms": ["intern", "contract"]}, "search": {}}
    titles = ["Senior Product Manager, Internal Tools", "Product Manager, International Payments",
              "Product Manager, Smart Contracts", "Product Manager Intern", "Contract Product Manager"]
    kept = [j.title for j in apply_filters([_job(t) for t in titles], cfg)]
    assert kept == titles[:3]


def test_ats_location_filter():
    cfg = {"search": {"locations": ["Los Angeles, CA", "Remote"], "country": "us"},
           "filters": {"nearby_locations": ["santa monica"]}}
    cases = {
        "Los Angeles, CA": True, "Santa Monica, CA": True, "Remote - US": True,
        "San Francisco, CA | New York, NY | Remote (US)": True, "": True,
        "London, UK": False, "Remote - UK": False, "San Francisco, CA": False,
        "Remote, EMEA": False,
    }
    for loc, keep in cases.items():
        got = apply_filters([_job(location=loc, source="greenhouse:acme")], cfg)
        assert bool(got) == keep, loc
    # aggregators were already searched per location: never dropped here
    assert apply_filters([_job(location="San Francisco, CA", source="adzuna")], cfg)


def test_lever_description_includes_requirement_lists():
    posting = {
        "descriptionPlain": "We build audio.",
        "lists": [{"text": "What you'll need", "content": "<li>7+ years of product management experience</li>"}],
        "additionalPlain": "Benefits.",
    }
    d = _lever_description(posting)
    assert "7+ years of product management experience" in d and "<li>" not in d
