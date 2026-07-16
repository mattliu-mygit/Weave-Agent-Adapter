"""Payload-free local diagnostics for hook and sidecar failures."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os


def diagnostic_path() -> str:
    return os.path.expanduser(os.environ.get(
        "WEAVE_AGENT_ADAPTER_LOG", "~/.weave-agent-adapter/adapter.log"))


def _handler() -> RotatingFileHandler:
    path = diagnostic_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    handler = RotatingFileHandler(path, maxBytes=1_048_576, backupCount=2)
    os.chmod(path, 0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    return handler


def diagnose(phase: str, *, harness: str = None, event: str = None,
             error: BaseException = None) -> None:
    fields = [f"phase={phase}"]
    if harness:
        fields.append(f"harness={harness}")
    if event:
        fields.append(f"event={event}")
    if error is not None:
        fields.append(f"error={type(error).__name__}")
    handler = None
    try:
        handler = _handler()
        record = logging.LogRecord("weave-agent-adapter", logging.ERROR, "", 0,
                                   " ".join(fields), (), None)
        handler.emit(record)
    except Exception:
        pass
    finally:
        if handler is not None:
            handler.close()


def open_diagnostic_stream():
    """Return a protected append stream suitable for a detached sidecar."""
    handler = _handler()
    handler.close()
    return open(diagnostic_path(), "a")
