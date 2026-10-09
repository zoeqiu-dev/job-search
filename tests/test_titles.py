import pytest
import yaml
from pathlib import Path

from agent.titles import expand, parse, rank, score_title
from agent.sources import Job

CFG = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())
TARGETS = CFG["candidate"]["target_titles"]
BOOSTS = CFG["filters"]["boost_terms"]
FLOOR = CFG["search"]["title_match_floor"]

REAL = [
    "Senior Product Manager", "Sr. PM, ML Infra", "Principal Product Manager, AI Platform",
    "Product Manager, AI", "Group Product Manager, Developer Experience",
    "Staff Product Manager - Machine Learning", "Senior Product Manager, Internal Tools",
    "Product Manager, Developer Tools", "Lead Product Manager, Infrastructure",
    "Technical Product Manager, LLM Platform", "Director of Product Management, AI",
    "Senior Manager, Product Management", "Product Lead, Generative AI",
    "Product Manager II, Data Platform", "Head of Product, Developer Platform",
    "AI Product Manager", "Senior Product Manager (Remote)", "Product Manager - Payments",
    "Director, Product", "Senior Manager, Product - Payments",
]

NOISE = [
    "Product Marketing Manager", "Senior Product Marketing Manager, AI",
    "Technical Program Manager", "Senior TPM, ML Infrastructure",
    "Engineering Manager, Product Platform", "Product Owner", "Senior Product Owner, Payments",
    "Product Designer", "Senior Product Designer, AI", "Data Center Architect",
    "Software Engineer, Product", "Senior Software Engineer, ML Platform", "Product Analyst",
    "Product Operations Manager", "Account Manager", "Customer Success Manager",
    "Associate Product Manager", "Product Manager Intern", "Program Manager, AI Safety",
    "Project Manager", "Product Specialist", "Product Counsel", "Recruiter, Product",
    "Solutions Architect", "Machine Learning Engineer", "Product Support Specialist",
    "Developer Advocate", "Technical Writer, Developer Docs", "Partner Manager, AI",
    "Research Scientist", "Marketing Manager, Product", "Sales Engineer", "APM, Growth",
]


@pytest.mark.parametrize("title", REAL)
def test_real_roles_kept(title):
    score, why = score_title(title, TARGETS, BOOSTS)
    assert score >= FLOOR, f"{title} scored {score}: {why}"


@pytest.mark.parametrize("title", NOISE)
def test_noise_rejected(title):
    score, why = score_title(title, TARGETS, BOOSTS)
    assert score < FLOOR, f"{title} scored {score}: {why}"


def test_abbreviations_expanded():
    assert expand("Sr. PM, ML Infra") == "senior product manager machine learning infrastructure"
    assert expand("TPM") == "technical program manager"


def test_function_judged_on_head_not_qualifier():
    assert parse("Engineering Manager, Product Platform").function == "engineering"
    assert parse("Product Manager, Engineering Platform").function == "product_management"
    assert parse("Manager, Product Management").function == "product_management"


def test_step_down_penalised_harder_than_stretch_up():
    targets = ["Senior Product Manager"]
    down, _ = score_title("Product Manager", targets)       # one level below
    up, _ = score_title("Staff Product Manager", targets)   # one level above
    assert up > down


def test_boost_does_not_rescue_wrong_function():
    assert score_title("Data Center Architect, AI Infrastructure", TARGETS, BOOSTS)[0] == 0


def test_domain_match_ranks_above_generic():
    ai, _ = score_title("Product Manager, AI", TARGETS, BOOSTS)
    plain, _ = score_title("Product Manager, Payments", TARGETS, BOOSTS)
    assert ai > plain


def test_rank_orders_and_drops():
    jobs = [Job(t, "Acme", url="u") for t in
            ["Data Center Architect", "Product Manager, Payments", "Senior Product Manager, AI"]]
    kept, dropped = rank(jobs, CFG)
    assert [j.title for j in kept] == ["Senior Product Manager, AI", "Product Manager, Payments"]
    assert [j.title for j in dropped] == ["Data Center Architect"]
    assert kept[0].extra["title_score"] >= kept[1].extra["title_score"]
