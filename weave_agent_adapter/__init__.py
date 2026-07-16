"""Harness-neutral Weave agent tracing for coding-agent sessions."""

from importlib import metadata

try:
    __version__ = metadata.version("weave-agent-adapter")
except metadata.PackageNotFoundError:
    __version__ = "0+unknown"
