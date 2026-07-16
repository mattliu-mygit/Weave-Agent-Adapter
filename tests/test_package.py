"""Package metadata has one authoritative version."""
from importlib import metadata

import weave_agent_adapter


def test_package_version_matches_installed_project_metadata():
    assert weave_agent_adapter.__version__ == metadata.version("weave-agent-adapter")
