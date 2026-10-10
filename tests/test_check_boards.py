import shutil
from pathlib import Path
from types import SimpleNamespace as NS

import check_boards

ROOT = Path(__file__).resolve().parent.parent


def test_add_to_config_keeps_comments_and_skips_duplicates(tmp_path):
    cfg = tmp_path / "config.yaml"
    shutil.copy(ROOT / "config.yaml", cfg)
    before = cfg.read_text()
    assert check_boards.add_to_config("greenhouse", "figma", cfg)
    assert not check_boards.add_to_config("greenhouse", "figma", cfg)
    assert check_boards.add_to_config("ashby", "notion", cfg)

    import yaml
    data = yaml.safe_load(cfg.read_text())
    assert data["ats"]["greenhouse"][-1] == "figma" and "notion" in data["ats"]["ashby"]
    # everything else, comments included, is untouched
    added = set(cfg.read_text().splitlines()) - set(before.splitlines())
    assert added == {"    - figma", "    - notion"}


def test_add_only_tokens_that_respond(tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "config.yaml"
    shutil.copy(ROOT / "config.yaml", cfg)
    monkeypatch.setattr(check_boards, "ROOT", tmp_path)

    def fake_get(url, **kw):
        if "greenhouse" in url and "/figma/" in url:
            return NS(status_code=200, json=lambda: {"jobs": [
                {"title": "PM", "location": {"name": "San Francisco, CA"}},
                {"title": "PM", "location": {"name": "London, UK"}}]})
        return NS(status_code=404, json=lambda: {})

    monkeypatch.setattr(check_boards.requests, "get", fake_get)
    monkeypatch.setattr("sys.argv", ["check_boards.py", "--add", "figma", "nosuchco"])
    check_boards.main()
    out = capsys.readouterr().out
    assert "1 in CA or remote" in out
    assert "Added to config.yaml: greenhouse:figma" in out
    import yaml
    ats = yaml.safe_load(cfg.read_text())["ats"]
    assert "figma" in ats["greenhouse"] and "nosuchco" not in str(ats)
