"""End-to-end with a fake Anthropic client: checks what each stage sends,
not what a real model would say. No network, no API key."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS

import yaml

from agent import generate as gen
from agent import llm, research, run, sources
from agent.sources import Job

ROOT = Path(__file__).resolve().parent.parent

PACKET_TEXT = "\n".join(
    f"=== FILE: {name} ===\n# {name}\nbody of {name}\n" for name in gen.DELIVERABLES
) + "=== END ==="


def _usage():
    return NS(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0,
              cache_read_input_tokens=0, server_tool_use=NS(web_search_requests=2))


class FakeClient:
    """Records every stream() call; answers research with notes and generate
    with a packet. The first research call pauses to exercise pause_turn."""

    def __init__(self):
        self.calls = []
        self.paused = False
        self.beta = NS(messages=NS(stream=self._stream))
        self.files = NS(retrieve_metadata=None, download=None)

    @contextmanager
    def _stream(self, **kw):
        self.calls.append(kw)
        is_generate = "container" in kw
        if not is_generate and not self.paused:
            self.paused = True
            msg = NS(stop_reason="pause_turn", usage=_usage(), container=None,
                     content=[NS(type="text", text="## Company\nAcme makes widgets.\n")])
        else:
            text = PACKET_TEXT if is_generate else "## Sources\n- https://acme.example\n"
            msg = NS(stop_reason="end_turn", usage=_usage(), container=NS(id="cont_1"),
                     content=[NS(type="text", text=text)])
        yield NS(get_final_message=lambda: msg)


def _job():
    j = Job("Senior Product Manager, AI Platform", "Acme", location="Remote - US",
            url="https://jobs.example/1", source="greenhouse:acme",
            description=("Own the roadmap for our ML platform. Write PRDs. Run customer "
                         "interviews. Developer tools and infrastructure. ") * 5
                        + "Requirements: 5+ years of product management experience.")
    j.extra["title_score"] = 90
    return j


def test_research_uses_web_search_and_resumes_pause_turn():
    c = FakeClient()
    notes = research.research(c, "claude-opus-5-5", _job())
    assert len(c.calls) == 2, "pause_turn should trigger one continuation"
    first, second = c.calls
    assert first["tools"][0]["type"].startswith("web_search")
    assert "container" not in first
    assert second["messages"][-1]["role"] == "assistant"
    assert "UNVERIFIED" in first["messages"][0]["content"]
    assert "Acme makes widgets" in notes and "acme.example" in notes


def test_generate_runs_skill_offline_with_research_as_text(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_SKILL_ID", "skill_test123")
    c = FakeClient()
    c.paused = True
    written = gen.generate(c, "claude-opus-5-5", _job(), "UNVERIFIED: they are hiring 50 PMs",
                           "# Zoe\nBase resume", tmp_path)
    (call,) = c.calls
    assert call["container"] == {"skills": [{"type": "custom", "skill_id": "skill_test123",
                                             "version": "latest"}]}
    tool_types = [t["type"] for t in call["tools"]]
    assert tool_types == [gen.CODE_EXECUTION_TOOL["type"]], "no network tool in generation"
    prompt = call["messages"][0]["content"]
    for rule in ["Do not invent employers, job titles, dates, degrees",
                 '"UNVERIFIED:" must not be used',
                 "genuine weak spots"]:
        assert rule in prompt
    assert "UNVERIFIED: they are hiring 50 PMs" in prompt and "Base resume" in prompt
    assert sorted(p.name for p in written) == sorted(gen.DELIVERABLES)
    assert (tmp_path / "brief.md").read_text().startswith("# brief.md")


def test_save_from_text_ignores_unknown_names(tmp_path):
    text = "pre\n=== FILE: ../../etc/passwd ===\nx\n=== FILE: brief.md ===\nok\n=== END ===\n"
    written = gen.save_from_text(text, tmp_path)
    assert [p.name for p in written] == ["brief.md"]


def _setup_root(tmp_path, monkeypatch, **search):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    cfg["search"].update(search)
    cfg["qualifications"]["years_experience"] = 6
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    (tmp_path / "base_resume.md").write_text("# Zoe\nresume")
    monkeypatch.setattr(run, "ROOT", tmp_path)
    noise = Job("Data Center Architect", "Acme", url="https://jobs.example/2",
                source="greenhouse:acme", description="x" * 300)
    monkeypatch.setattr(sources, "collect", lambda cfg: [_job(), noise])


def test_dry_run_makes_no_api_calls(tmp_path, monkeypatch, capsys):
    _setup_root(tmp_path, monkeypatch)
    monkeypatch.setattr(llm, "client", lambda: (_ for _ in ()).throw(AssertionError("API used")))
    assert run.main(["--dry-run", "--config", str(tmp_path / "config.yaml")]) == 0
    out = capsys.readouterr().out
    assert "https://jobs.example/1" in out and "jobs.example/2" not in out
    assert not (tmp_path / "out").exists()


def test_full_run_lists_urls_builds_packet_and_remembers(tmp_path, monkeypatch, capsys):
    _setup_root(tmp_path, monkeypatch, max_packets_per_run=1)
    monkeypatch.setenv("RESUME_SKILL_ID", "skill_test123")
    fake = FakeClient()
    monkeypatch.setattr(llm, "client", lambda: fake)
    assert run.main(["--config", str(tmp_path / "config.yaml")]) == 0

    day = next((tmp_path / "out").iterdir())
    matches = (day / "matches.md").read_text()
    assert "https://jobs.example/1" in matches and "Data Center" not in matches
    packet = next(p for p in day.iterdir() if p.is_dir())
    assert {"research.md", "posting.md", "brief.md", "resume_final.md"} <= {p.name for p in packet.iterdir()}
    assert "Estimated spend this run" in capsys.readouterr().out

    # Second run: already seen, so nothing is screened or paid for again.
    fake.calls.clear()
    run.main(["--config", str(tmp_path / "config.yaml")])
    assert fake.calls == []


def test_hybrid_only_sends_unblocked_postings_to_haiku(tmp_path, monkeypatch, capsys):
    _setup_root(tmp_path, monkeypatch)
    cfg = yaml.safe_load((tmp_path / "config.yaml").read_text())
    cfg["screen"]["mode"] = "hybrid"
    cfg["search"]["min_score_to_generate"] = 101   # screen only, no packets
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    blocked = Job("Senior Product Manager, Defense", "Acme", url="https://jobs.example/3",
                  source="greenhouse:acme",
                  description=("ML platform roadmap PRD. " * 20) + "Active TS/SCI clearance required.")
    monkeypatch.setattr(sources, "collect", lambda cfg: [_job(), blocked])

    sent = []

    def create(**kw):
        sent.append(kw["messages"][0]["content"])
        fp = _job().fingerprint
        return NS(usage=_usage(), content=[NS(type="text", text=(
            f'[{{"id": "{fp}", "score": 88, "reason": "strong fit", "blockers": ["pay unstated"]}}]'))])

    fake = FakeClient()
    fake.messages = NS(create=create)
    monkeypatch.setattr(llm, "client", lambda: fake)
    run.main(["--config", str(tmp_path / "config.yaml")])

    assert len(sent) == 1 and "Defense" not in sent[0], "rule-blocked posting reached Haiku"
    matches = next((tmp_path / "out").iterdir()).joinpath("matches.md").read_text()
    # Haiku's soft "blockers" don't remove a posting it scored 88
    assert "https://jobs.example/1" in matches and "jobs.example/3" not in matches
    assert "strong fit" in matches
