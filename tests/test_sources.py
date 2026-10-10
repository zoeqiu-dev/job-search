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


CA_CFG = {"location": {"state": "CA", "allow_remote": True, "unknown": "drop"}}


@pytest.mark.parametrize("loc, verdict", [
    ("Los Angeles, CA", "in"),
    ("San Francisco, California, United States", "in"),
    ("Santa Monica", "in"),
    ("Mountain View, CA 94043", "in"),
    ("San Francisco Bay Area", "in"),
    ("Santa Clara or San Jose", "in"),
    ("Los Angeles, CA (+2 others)", "in"),
    ("San Francisco, CA; New York, NY", "in"),        # any office in CA is enough
    ("Remote - California", "in"),
    ("Remote - US", "remote"),
    ("United States (Remote)", "remote"),
    ("Anywhere", "remote"),
    ("New York, NY | Remote (US)", "remote"),
    ("New York, NY", "out"),
    ("Austin, TX or New York, NY", "out"),
    ("Portland, OR", "out"),
    ("Seattle, Washington", "out"),
    ("Washington, DC", "out"),
    ("Pasadena, TX", "out"),                          # CA city name, other state
    ("Richmond, VA", "out"),
    ("Remote - New York", "out"),                     # remote, but limited to NY
    ("Remote - UK", "out"),
    ("Remote, EMEA", "out"),
    ("London, UK", "out"),
    ("Toronto, ON, CA", "out"),                       # CA = Canada here
    ("Vancouver, BC", "out"),
    ("Hybrid", "unknown"),
    ("", "unknown"),
])
def test_location_verdict(loc, verdict):
    from agent.sources import location_verdict
    assert location_verdict(loc, "ca") == verdict


def test_california_filter_applies_to_every_source():
    for source in ["adzuna", "google_jobs", "greenhouse:acme", "lever:acme", "ashby:acme"]:
        assert apply_filters([_job(location="Irvine, CA", source=source)], CA_CFG)
        assert not apply_filters([_job(location="Austin, TX", source=source)], CA_CFG)


def test_remote_can_be_switched_off():
    cfg = {"location": {"state": "CA", "allow_remote": False}}
    assert not apply_filters([_job(location="Remote - US", source="adzuna")], cfg)
    assert apply_filters([_job(location="Remote - California", source="adzuna")], cfg)


def test_unknown_location_policy():
    j = lambda **kw: _job(location="", **kw)
    assert not apply_filters([j(source="adzuna")], CA_CFG)
    assert apply_filters([j(source="adzuna")], {"location": {"state": "CA", "unknown": "keep"}})
    # alert emails carry no location; the alert itself was set up with one
    assert apply_filters([j(source="linkedin_alert_email")], CA_CFG)


def test_extra_cities_and_no_state_means_no_check():
    cfg = {"location": {"state": "California", "extra_cities": ["Covina"]}}
    assert apply_filters([_job(location="Covina")], cfg)
    assert apply_filters([_job(location="Austin, TX")], {"location": {"state": ""}})


def test_queries_follow_titles():
    from agent.sources import queries
    cfg = {"search": {"keywords": ["senior product manager AI", "PM developer tools"],
                      "keywords_from_titles": True},
           "candidate": {"target_titles": ["Senior Product Manager, AI", "Chief of Staff"]}}
    # "Senior Product Manager, AI" duplicates the first keyword once commas/case are ignored
    assert queries(cfg) == ["senior product manager AI", "PM developer tools", "Chief of Staff"]
    cfg["search"]["keywords_from_titles"] = False
    assert queries(cfg) == ["senior product manager AI", "PM developer tools"]


def test_adzuna_location_gets_state_from_area():
    from agent.sources import _adzuna_location
    loc = {"display_name": "Santa Monica, Los Angeles County",
           "area": ["US", "California", "Los Angeles County", "Santa Monica"]}
    assert _adzuna_location(loc) == "Santa Monica, Los Angeles County, California"
    assert _adzuna_location({"display_name": "Austin, Texas", "area": ["US", "Texas"]}) == "Austin, Texas"


def test_lever_description_includes_requirement_lists():
    posting = {
        "descriptionPlain": "We build audio.",
        "lists": [{"text": "What you'll need", "content": "<li>7+ years of product management experience</li>"}],
        "additionalPlain": "Benefits.",
    }
    d = _lever_description(posting)
    assert "7+ years of product management experience" in d and "<li>" not in d
