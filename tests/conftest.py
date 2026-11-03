"""Shared test setup."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _private_diagnostics_log(tmp_path, monkeypatch):
    """Keep test runs out of the user's real adapter.log."""
    monkeypatch.setenv("WEAVE_AGENT_ADAPTER_LOG", str(tmp_path / "adapter.log"))
