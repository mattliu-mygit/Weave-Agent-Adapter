"""Config-surface fingerprint: a stable short hash of the user-editable context
artifacts (CLAUDE.md, skills, commands) so evaluation cohorts can be
compared across config changes (the A/B key)."""
from __future__ import annotations

from weave_agent_adapter.config_surface import config_version


def test_stable_and_ignores_missing_paths(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("be concise")
    paths = [str(f), str(tmp_path / "does-not-exist")]
    v1 = config_version(paths)
    assert v1 is not None
    assert config_version(paths) == v1                    # deterministic
    assert len(v1) == 12                                  # short hex fingerprint


def test_changes_when_content_changes(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("be concise")
    before = config_version([str(f)])
    f.write_text("be verbose")
    assert config_version([str(f)]) != before


def test_none_when_nothing_exists(tmp_path):
    assert config_version([str(tmp_path / "missing"), str(tmp_path / "gone")]) is None


def test_directory_walk_covers_nested_files(tmp_path):
    skills = tmp_path / "skills"
    (skills / "a").mkdir(parents=True)
    (skills / "a" / "SKILL.md").write_text("skill a")
    (skills / "b.md").write_text("skill b")
    before = config_version([str(skills)])
    (skills / "a" / "SKILL.md").write_text("skill a v2")  # nested change is seen
    after = config_version([str(skills)])
    assert before != after
    (skills / "c.md").write_text("new skill")             # added file is seen
    assert config_version([str(skills)]) != after


def test_noise_files_do_not_affect_hash(tmp_path):
    d = tmp_path / "commands"
    d.mkdir()
    (d / "go.md").write_text("run")
    before = config_version([str(d)])
    (d / ".DS_Store").write_text("junk")
    pycache = d / "__pycache__"
    pycache.mkdir()
    (pycache / "x.pyc").write_text("bytecode")
    assert config_version([str(d)]) == before


def test_cwd_and_slug_placeholders(tmp_path):
    proj = tmp_path / "repo"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("project rules")
    # {cwd} expands to the session cwd
    assert config_version(["{cwd}/CLAUDE.md"], cwd=str(proj)) is not None
    # {cwd_slug} expands to the cwd with "/" replaced by "-" (Claude Code's
    # ~/.claude/projects/<slug> convention)
    slug_dir = tmp_path / str(proj).replace("/", "-")
    slug_dir.mkdir()
    (slug_dir / "MEMORY.md").write_text("memory")
    tpl = str(tmp_path) + "/{cwd_slug}/MEMORY.md"
    assert config_version([tpl], cwd=str(proj)) is not None
    assert config_version([tpl], cwd="/elsewhere") is None


def test_claude_code_surface_excludes_ambient_memory():
    # config_version is the A/B cohort key: it must cover only deliberately
    # edited artifacts. Auto-memory changes nearly every session — including
    # it would flip the version constantly and shatter cohorts.
    from weave_agent_adapter.profile import load_profile

    paths = load_profile("claude-code").config_surface["paths"]
    assert not any("memory" in p for p in paths)
    assert any(p.endswith("/skills") for p in paths)


def _codex_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    codex = home / ".codex"
    (codex / "skills" / "review").mkdir(parents=True)
    (codex / "skills" / "review" / "SKILL.md").write_text("review skill")
    (codex / "AGENTS.md").write_text("be concise")
    (codex / "config.toml").write_text('model = "gpt-5"\n')
    (codex / "memories").mkdir()
    (codex / "memories" / "note.md").write_text("remembered")
    monkeypatch.setenv("HOME", str(home))
    return codex


def test_codex_surface_tracks_agents_md_but_not_config_toml_or_memories(tmp_path, monkeypatch):
    from weave_agent_adapter.profile import load_profile

    codex = _codex_home(tmp_path, monkeypatch)
    paths = load_profile("codex").config_surface["paths"]
    before = config_version(paths)
    assert before is not None

    # config.toml churns with trust lists, MCP, and auth; memories change every session.
    (codex / "config.toml").write_text('model = "gpt-5"\n[projects."/repo"]\ntrust_level = "trusted"\n')
    (codex / "memories" / "note.md").write_text("remembered more")
    (codex / "state_5.sqlite").write_bytes(b"rows")
    assert config_version(paths) == before

    (codex / "AGENTS.md").write_text("be verbose")
    assert config_version(paths) != before


def test_codex_surface_includes_the_session_cwd_agents_md(tmp_path, monkeypatch):
    from weave_agent_adapter.profile import load_profile

    _codex_home(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = load_profile("codex").config_surface["paths"]
    without = config_version(paths, cwd=str(repo))
    (repo / "AGENTS.md").write_text("repo rules")
    with_repo = config_version(paths, cwd=str(repo))
    assert with_repo != without
    (repo / "AGENTS.md").write_text("repo rules v2")
    assert config_version(paths, cwd=str(repo)) != with_repo
