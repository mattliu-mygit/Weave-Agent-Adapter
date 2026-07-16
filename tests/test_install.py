"""Installer: settings.json merge/idempotency and plugin generation."""
from __future__ import annotations

import json
import os
import shutil

import pytest

from weave_agent_adapter.install import install, uninstall, write_plugin
from weave_agent_adapter.profile import load_profile


def _read(p):
    with open(p) as f:
        return json.load(f)


def test_install_wires_all_events(tmp_path):
    path = str(tmp_path / "settings.json")
    install("claude-code", path=path)
    hooks = _read(path)["hooks"]
    for ev in ("SessionStart", "PreToolUse", "SubagentStop", "PreCompact", "SessionEnd"):
        assert ev in hooks
        cmd = hooks[ev][0]["hooks"][0]["command"]
        assert cmd.endswith(f"--event {ev}")


def test_install_is_idempotent(tmp_path):
    path = str(tmp_path / "settings.json")
    install("claude-code", path=path)
    install("claude-code", path=path)                 # twice
    hooks = _read(path)["hooks"]
    assert len(hooks["PreToolUse"]) == 1               # not duplicated


def test_install_preserves_foreign_hooks(tmp_path):
    path = str(tmp_path / "settings.json")
    with open(path, "w") as f:
        json.dump({"hooks": {"PreToolUse": [
            {"hooks": [{"type": "command", "command": "someone-elses-hook"}]}]}}, f)
    install("claude-code", path=path)
    cmds = [h["command"] for e in _read(path)["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert "someone-elses-hook" in cmds                # foreign entry kept
    assert any("weave-agent-adapter" in c for c in cmds)


def test_uninstall_removes_only_ours(tmp_path):
    path = str(tmp_path / "settings.json")
    with open(path, "w") as f:
        json.dump({"hooks": {"PreToolUse": [
            {"hooks": [{"type": "command", "command": "someone-elses-hook"}]}]}}, f)
    install("claude-code", path=path)
    uninstall("claude-code", path=path)
    hooks = _read(path).get("hooks", {})
    cmds = [h["command"] for e in hooks.get("PreToolUse", []) for h in e["hooks"]]
    assert cmds == ["someone-elses-hook"]


def test_install_resolves_target_from_profile_no_code(tmp_path):
    # a brand-new harness installs with only a profile: the registration names
    # its own target path, so no installer code (no target map) is touched.
    prof_dir = tmp_path / "profiles"
    prof_dir.mkdir()
    target = tmp_path / "myh-hooks.json"
    (prof_dir / "myh.toml").write_text(
        '[harness]\nname = "myh"\nadapter = "command-hook"\n'
        '[events]\nSessionStart = "session_start"\nPreToolUse = "tool_pre"\n'
        "[registration]\n"
        f'user_path = "{target}"\n'
        'local_path = ".myh/hooks.json"\n'
        'command = "weave-agent-adapter hook --harness myh"\n'
        'events = ["SessionStart", "PreToolUse"]\n'
    )
    p = install("myh", user=True, profiles_dir=str(prof_dir))   # no explicit path
    assert p == str(target)
    hooks = _read(str(target))["hooks"]
    assert set(hooks) == {"SessionStart", "PreToolUse"}


def test_write_plugin_emits_manifest_and_hooks(tmp_path):
    dest = str(tmp_path / "plugin")
    write_plugin("claude-code", dest)
    manifest = _read(os.path.join(dest, ".claude-plugin", "plugin.json"))
    assert manifest["name"] == "weave-agent-adapter"
    hooks = _read(os.path.join(dest, "hooks", "hooks.json"))["hooks"]
    assert "SessionStart" in hooks and "PreCompact" in hooks


def test_install_refuses_to_overwrite_malformed_json(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ broken")
    with pytest.raises(ValueError, match="valid JSON"):
        install("claude-code", path=str(path))
    assert path.read_text() == "{ broken"


def test_install_uses_resolved_console_script(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/opt/bin/weave-agent-adapter")
    path = str(tmp_path / "hooks.json")
    install("codex", path=path)
    command = _read(path)["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command.startswith("/opt/bin/weave-agent-adapter hook --harness codex")


def test_atomic_write_preserves_old_file_when_replace_fails(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    original = '{"foreign": true}\n'
    path.write_text(original)

    def fail_replace(src, dst):
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="boom"):
        install("claude-code", path=str(path))
    assert path.read_text() == original
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


def test_checked_in_claude_plugin_events_match_profile():
    checked_in = _read("plugin/claude-code/hooks/hooks.json")["hooks"]
    expected = set(load_profile("claude-code").registration["events"])
    assert set(checked_in) == expected
