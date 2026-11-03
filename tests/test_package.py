"""Package metadata has one authoritative version."""
from importlib import metadata
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

import weave_agent_adapter


def test_package_version_matches_installed_project_metadata():
    assert weave_agent_adapter.__version__ == metadata.version("weave-agent-adapter")


def test_runtime_needs_no_third_party_package():
    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    project = data["project"]
    assert project["requires-python"] == ">=3.10"
    assert project["dependencies"] == ["tomli; python_version < '3.11'"]
    assert project["optional-dependencies"] == {"dev": ["pytest>=7"]}
