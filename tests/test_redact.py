"""Redactor: sensitive-key denylist and secret-shaped pattern scrubbing."""
from __future__ import annotations

from weave_agent_adapter.redact import REDACTED, Redactor


def test_deny_key_drops_whole_value():
    r = Redactor()
    out = r.scrub({"authorization": "Bearer abc", "command": "ls"})
    assert out["authorization"] == REDACTED
    assert out["command"] == "ls"


def test_secret_shaped_patterns_scrubbed_in_strings():
    r = Redactor()
    assert r.scrub("key is sk-ABCDEFGHIJKLMNOP1234") == "key is " + REDACTED
    assert REDACTED in r.scrub("token wandb_v1_" + "A" * 30)
    assert REDACTED in r.scrub("AKIA" + "0123456789ABCDEF")


def test_nested_and_list_recursion():
    r = Redactor()
    out = r.scrub({"outer": {"password": "hunter2", "keep": [{"secret": "x"}, "plain"]}})
    assert out["outer"]["password"] == REDACTED
    assert out["outer"]["keep"][0]["secret"] == REDACTED
    assert out["outer"]["keep"][1] == "plain"


def test_disabled_passes_through():
    r = Redactor(enabled=False)
    assert r.scrub({"api_key": "secret"}) == {"api_key": "secret"}


def test_custom_deny_keys_extend_defaults():
    r = Redactor(deny_keys={"ssn"})
    out = r.scrub({"ssn": "123", "api_key": "also-denied", "command": "ls"})
    assert out["ssn"] == REDACTED
    assert out["api_key"] == REDACTED       # defaults still active
    assert out["command"] == "ls"
