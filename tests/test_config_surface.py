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


def _linked_skills(tmp_path):
    # Skills kept elsewhere and linked into the folder the profile names, as
    # ~/.codex/skills links into ~/.claude/skills.
    shared = tmp_path / "shared"
    (shared / "review").mkdir(parents=True)
    (shared / "review" / "SKILL.md").write_text("review v1")
    (shared / "plan.md").write_text("plan v1")
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "local.md").write_text("local skill")
    (skills / "review").symlink_to(shared / "review", target_is_directory=True)
    (skills / "plan.md").symlink_to(shared / "plan.md")
    return shared, skills


def test_edit_behind_a_symlinked_skill_file_changes_the_version(tmp_path):
    shared, skills = _linked_skills(tmp_path)
    before = config_version([str(skills)])
    (shared / "plan.md").write_text("plan v2")
    assert config_version([str(skills)]) != before


def test_edit_inside_a_symlinked_skill_directory_changes_the_version(tmp_path):
    shared, skills = _linked_skills(tmp_path)
    before = config_version([str(skills)])
    (shared / "review" / "SKILL.md").write_text("review v2")
    assert config_version([str(skills)]) != before


def test_moving_a_symlink_target_with_the_same_content_keeps_the_version(tmp_path):
    shared, skills = _linked_skills(tmp_path)
    before = config_version([str(skills)])
    moved = tmp_path / "moved"
    shared.rename(moved)
    (skills / "review").unlink()
    (skills / "review").symlink_to(moved / "review", target_is_directory=True)
    (skills / "plan.md").unlink()
    (skills / "plan.md").symlink_to(moved / "plan.md")
    assert config_version([str(skills)]) == before


def test_a_symlink_loop_ends_and_each_file_counts_once(tmp_path):
    skills = tmp_path / "skills"
    (skills / "a").mkdir(parents=True)
    (skills / "a" / "SKILL.md").write_text("skill a")
    (skills / "a" / "up").symlink_to(skills, target_is_directory=True)      # a/up -> skills
    looped = config_version([str(skills)])
    (skills / "a" / "up").unlink()
    assert looped == config_version([str(skills)])


def test_excluded_paths_do_not_affect_the_version(tmp_path):
    skills = tmp_path / "skills"
    (skills / "managed").mkdir(parents=True)
    (skills / "managed" / "SKILL.md").write_text("v1")
    (skills / "mine.md").write_text("mine")
    exclude = [str(skills / "managed")]
    before = config_version([str(skills)], exclude=exclude)
    (skills / "managed" / "SKILL.md").write_text("v2")
    (skills / "managed" / "new.md").write_text("new")
    assert config_version([str(skills)], exclude=exclude) == before
    (skills / "mine.md").write_text("mine v2")
    assert config_version([str(skills)], exclude=exclude) != before


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


def test_claude_code_surface_ignores_investigate_notes(tmp_path, monkeypatch):
    # Mirrors wandb/core: .claude/skills links to ../.agents/skills, and the
    # investigate skill's observations/ links to a notes folder outside the repo.
    from weave_agent_adapter.profile import load_profile

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    skill = repo / ".agents" / "skills" / "investigate"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("investigate v1")
    (repo / ".agents" / "skills" / "review").mkdir()
    (repo / ".agents" / "skills" / "review" / "SKILL.md").write_text("review")
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "first.md").write_text("first note")
    (skill / "observations").symlink_to(notes, target_is_directory=True)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "skills").symlink_to("../.agents/skills", target_is_directory=True)
    surface = load_profile("claude-code").config_surface

    def version():
        return config_version(surface["paths"], cwd=str(repo), exclude=surface.get("exclude"))

    before = version()
    assert before is not None
    (notes / "first.md").write_text("first note, edited")
    (notes / "second.md").write_text("second note")
    assert version() == before

    (skill / "SKILL.md").write_text("investigate v2")
    assert version() != before


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


def test_codex_surface_ignores_codex_managed_system_skills(tmp_path, monkeypatch):
    from weave_agent_adapter.profile import load_profile

    codex = _codex_home(tmp_path, monkeypatch)
    system = codex / "skills" / ".system"
    (system / "imagegen").mkdir(parents=True)
    (system / "imagegen" / "SKILL.md").write_text("bundled v1")
    surface = load_profile("codex").config_surface
    before = config_version(surface["paths"], exclude=surface.get("exclude"))

    # A Codex upgrade rewrites its bundled skills; the user edited nothing.
    (system / "imagegen" / "SKILL.md").write_text("bundled v2")
    (system / ".codex-system-skills.marker").write_text("0.2")
    assert config_version(surface["paths"], exclude=surface.get("exclude")) == before

    (codex / "skills" / "review" / "SKILL.md").write_text("review skill v2")
    assert config_version(surface["paths"], exclude=surface.get("exclude")) != before
