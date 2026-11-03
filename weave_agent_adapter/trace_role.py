"""Resolve trace classification from process or workspace-local state."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .model import DEFAULT_TRACE_ROLE, TRACE_ROLE_ENV, normalize_trace_role

LOCAL_STATE_DIR = ".weave-agent-adapter"
TRACE_ROLE_FILE = "trace-role"


def _start_directory(cwd: object) -> Path:
    try:
        path = Path(os.getcwd() if cwd is None else cwd).expanduser()
    except (TypeError, ValueError):
        path = Path(os.getcwd())
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def _workspace_directories(start: Path) -> tuple[Path, ...]:
    candidates = (start, *start.parents)
    for index, directory in enumerate(candidates):
        try:
            is_repository_root = (directory / ".git").exists()
        except OSError:
            is_repository_root = False
        if is_repository_root:
            return candidates[: index + 1]
    return (start,)


def resolve_trace_role(
    cwd: object = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve env > nearest workspace file > default, validating explicit values."""
    source = os.environ if environ is None else environ
    configured = source.get(TRACE_ROLE_ENV, "").strip()
    if configured:
        return normalize_trace_role(configured)

    start = _start_directory(cwd)
    for directory in _workspace_directories(start):
        selector = directory / LOCAL_STATE_DIR / TRACE_ROLE_FILE
        try:
            configured = selector.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            continue
        if configured:
            return normalize_trace_role(configured)

    return DEFAULT_TRACE_ROLE
