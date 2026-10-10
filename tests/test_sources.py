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


AREA_CFG = {"location": {"regions": ["bay_area", "los_angeles"], "allow_remote": True,
                         "unknown": "drop"}}


@pytest.mark.parametrize("loc, verdict", [
    # Bay Area, all nine counties
    ("San Francisco, CA", "in"),
    ("Mountain View, CA 94043", "in"),
    ("San Francisco Bay Area", "in"),
    ("Santa Clara or San Jose", "in"),
    ("Oakland, California", "in"),
    ("Santa Rosa, CA", "in"),
    ("Dublin, CA", "in"),                              # East Bay, not Ireland
    ("Seattle, WA; San Francisco, CA", "in"),          # any office in an area
    # LA County + Orange County
    ("Los Angeles, CA", "in"),
    ("Los Angeles, CA (+2 others)", "in"),
    ("Santa Monica", "in"),
    ("Pasadena", "in"),
    ("Covina, CA", "in"),
    ("Irvine, CA", "in"),
    ("Orange, CA", "in"),
    ("Greater Los Angeles Area", "in"),
    # US-remote
    ("Remote - US", "remote"),
    ("Remote - California", "remote"),
    ("United States (Remote)", "remote"),
    ("Anywhere", "remote"),
    ("New York, NY | Remote (US)", "remote"),
    # California, but not one of the areas
    ("San Diego, CA", "out"),
    ("Sacramento, CA", "out"),
    ("California", "out"),
    ("California, United States", "out"),
    # elsewhere
    ("New York, NY", "out"),
    ("Pasadena, TX", "out"),
    ("Orange, TX", "out"),
    ("Newark, NJ", "out"),
    ("Dublin, Ireland", "out"),
    ("Dublin", "out"),
    ("Portland, OR", "out"),
    ("Remote - New York", "out"),
    ("Remote - UK", "out"),
    ("Toronto, ON, CA", "out"),
    # can't tell
    ("Glendale", "unknown"),                           # CA or AZ
    ("Hybrid", "unknown"),
    ("", "unknown"),
])
def test_location_verdict(loc, verdict):
    from agent.sources import location_verdict, region_cities
    assert location_verdict(loc, region_cities(["bay_area", "los_angeles"])) == verdict


def test_area_filter_applies_to_every_source():
    for source in ["adzuna", "google_jobs", "greenhouse:acme", "lever:acme", "ashby:acme"]:
        assert apply_filters([_job(location="Palo Alto, CA", source=source)], AREA_CFG)
        assert not apply_filters([_job(location="San Diego, CA", source=source)], AREA_CFG)


def test_remote_can_be_switched_off():
    cfg = {"location": {"regions": ["bay_area"], "allow_remote": False}}
    assert not apply_filters([_job(location="Remote - US", source="adzuna")], cfg)
    assert apply_filters([_job(location="Sunnyvale, CA", source="adzuna")], cfg)


def test_one_region_only():
    cfg = {"location": {"regions": ["los_angeles"]}}
    assert apply_filters([_job(location="Culver City, CA")], cfg)
    assert not apply_filters([_job(location="San Francisco, CA")], cfg)


def test_unknown_region_is_a_clear_error():
    with pytest.raises(ValueError, match="bay_area"):
        apply_filters([_job()], {"location": {"regions": ["seattle"]}})


def test_unknown_location_policy():
    j = lambda **kw: _job(location="", **kw)
    assert not apply_filters([j(source="adzuna")], AREA_CFG)
    keep = {"location": {"regions": ["bay_area"], "unknown": "keep"}}
    assert apply_filters([j(source="adzuna")], keep)
    # alert emails carry no location; the alert itself was set up with one
    assert apply_filters([j(source="linkedin_alert_email")], AREA_CFG)


def test_extra_cities_and_no_regions_means_no_check():
    cfg = {"location": {"regions": ["bay_area"], "extra_cities": ["Monterey"]}}
    assert apply_filters([_job(location="Monterey, CA")], cfg)
    assert apply_filters([_job(location="Austin, TX")], {"location": {}})


@pytest.mark.parametrize("company, description, agency", [
    ("Robert Half", "", True),
    ("Robert Half Technology", "", True),
    ("Acme Staffing LLC", "", True),
    ("TalentHub Recruiting", "", True),
    ("Jobot", "", True),
    ("Acme", "On behalf of our client, a Series B AI startup, we seek a PM.", True),
    ("Stripe", "We're hiring a PM to own our payments roadmap.", False),
    ("Anthropic", "Our client success team partners with PMs.", False),
    ("Haystack", "", False),
    ("Volta", "", False),
    ("Placement Labs", "", False),
])
def test_staffing_agencies(company, description, agency):
    from agent.sources import is_agency
    assert is_agency(_job(company=company, description=description)) == agency


def test_agency_filter_can_be_extended_and_disabled():
    j = _job(company="Niche Talent Co")
    assert apply_filters([j], {"filters": {"extra_agencies": ["Niche Talent Co"]}}) == []
    assert apply_filters([_job(company="Jobot")], {"filters": {"drop_staffing_agencies": False}})


def test_search_areas_and_serpapi_place():
    from agent.sources import _serpapi_place, search_areas
    areas = search_areas({"search": {"locations": [
        {"name": "Bay Area", "near": "San Francisco, CA", "radius_km": 100}, "Remote"]}})
    assert areas[0] == {"name": "Bay Area", "near": "San Francisco, CA", "radius_km": 100,
                        "remote": False}
    assert areas[1]["remote"] is True
    assert _serpapi_place("San Francisco, CA") == "San Francisco, California, United States"


class _KV(dict):
    def get_kv(self, k): return self.get(k)
    def set_kv(self, k, v): self[k] = v


def test_rotation_covers_every_combination_across_runs(capsys):
    from agent.sources import Rotation
    combos = list(range(10))
    store, seen = _KV(), []
    for _ in range(4):                                   # ceil(10 / 3) runs
        seen += Rotation(store).plan("google_jobs", combos, 3)
    assert set(seen) == set(combos)
    assert "all covered every 4 runs" in capsys.readouterr().out
    assert Rotation(store).plan("adzuna", combos, 50) == combos   # budget ≥ combos: all
    assert Rotation(store).plan("adzuna", combos, None) == combos  # no cap


def test_dry_run_rotation_does_not_advance():
    from agent.sources import Rotation
    store = _KV()
    first = Rotation(store, save=False).plan("g", list(range(9)), 3)
    assert Rotation(store, save=False).plan("g", list(range(9)), 3) == first


def test_adzuna_sends_radius_and_respects_budget(monkeypatch):
    from agent import sources
    calls = []
    monkeypatch.setenv("ADZUNA_APP_ID", "x")
    monkeypatch.setenv("ADZUNA_APP_KEY", "y")
    monkeypatch.setattr(sources, "_get", lambda url, **kw: calls.append(kw["params"]))
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    cfg = {"search": {"keywords": ["pm ai", "pm platform"], "keywords_from_titles": False,
                      "country": "us", "max_days_old": 3,
                      "locations": [{"name": "LA", "near": "Los Angeles, CA", "radius_km": 80},
                                    "Remote"]},
           "sources": {"adzuna_calls_per_run": 3}}
    sources.from_adzuna(cfg, sources.Rotation(_KV()))
    assert len(calls) == 3
    assert calls[0]["where"] == "Los Angeles, CA" and calls[0]["distance"] == 80
    assert calls[1]["where"] == "" and calls[1]["what"] == "pm ai remote"


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
