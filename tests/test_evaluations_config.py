"""The `[evaluations]` config section and the setup state file."""
from __future__ import annotations

from weave_agent_adapter import config


def test_evaluations_section_is_off_by_default_and_parsed_when_present(tmp_path):
    assert config.load_config(str(tmp_path / "missing.toml")).evaluations.enabled is False
    path = tmp_path / "c.toml"
    path.write_text('[evaluations]\nenabled = true\nproject = "wandb/matt-test"\nharnesses = ["codex"]\n')
    ev = config.load_config(str(path)).evaluations
    assert (ev.enabled, ev.project, ev.harnesses, ev.base_url) == (
        True, "wandb/matt-test", ("codex",), "https://evaluations.wandb.ai")


def test_state_round_trips_and_is_none_before_setup(tmp_path):
    path = str(tmp_path / "evaluations.json")
    assert config.load_evaluations_state(path) is None
    config.save_evaluations_state({"scope_ref": "S", "dataset_id": "D"}, path)
    assert config.load_evaluations_state(path)["dataset_id"] == "D"


def test_defaults_point_at_prod_and_every_harness(tmp_path):
    ev = config.load_config(str(tmp_path / "missing.toml")).evaluations
    assert (ev.base_url, ev.wandb_base_url, ev.harnesses) == (
        "https://evaluations.wandb.ai", "https://api.wandb.ai", ("claude-code", "codex", "gemini-cli"))


def test_rollout_context_is_off_unless_configured(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[evaluations]\nrollout_context = true\n')

    assert config.load_config(str(tmp_path / "missing.toml")).evaluations.rollout_context is False
    assert config.load_config(str(path)).evaluations.rollout_context is True
