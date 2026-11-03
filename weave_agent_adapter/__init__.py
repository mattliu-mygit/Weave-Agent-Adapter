"""Harness-neutral recording of coding-agent sessions in CoreWeave Evaluations."""

from importlib import metadata

try:
    __version__ = metadata.version("weave-agent-adapter")
except metadata.PackageNotFoundError:
    __version__ = "0+unknown"
