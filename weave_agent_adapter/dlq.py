"""Dead-letter file for CES turn jobs that failed, and their replay.

One JSON object per line, each holding a whole turn job (format v2). Replay
reruns `CesWriter.write_turn` from the top: requests that already finished
replay with their original IDs, because each has the same key and body. The file holds redacted payload values, so it is
created user-only. Replay first renames the file, so the sidecar keeps appending
to a fresh file while old lines are resent. One replay runs at a time, guarded by
a lock on a separate `.lock` file; lines that cannot be parsed move to `.corrupt`.
"""
from __future__ import annotations

import fcntl
import json
import os
import time

VERSION = 2
DEFAULT_PATH = os.path.expanduser(os.environ.get(
    "WEAVE_AGENT_ADAPTER_EVALUATIONS_DLQ", "~/.weave-agent-adapter/evaluations-dlq.jsonl"))


class ReplayInProgress(Exception):
    """Another replay holds the lock on this DLQ."""


def record(job: dict, key: str, status, error_code, attempts: int) -> dict:
    """`key` names the turn, such as `turn-v1:codex:<session>:<turn>`, for reports."""
    return {"v": VERSION, "failed_at": time.time(), "key": key, "job": job, "status": status,
            "error_code": error_code, "attempts": attempts}


def append(dlq_path: str, entries: list) -> None:
    os.makedirs(os.path.dirname(dlq_path) or ".", mode=0o700, exist_ok=True)
    while True:
        fd = os.open(dlq_path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                current = os.stat(dlq_path).st_ino == os.fstat(fh.fileno()).st_ino
            except FileNotFoundError:
                current = False
            if not current:
                continue                      # replay moved the file while we waited
            os.fchmod(fh.fileno(), 0o600)
            size = os.fstat(fh.fileno()).st_size
            if size and os.pread(fh.fileno(), 1, size - 1) != b"\n":
                fh.write("\n")                # end a torn tail so it cannot absorb the next entry
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
            return


def _take(dlq_path: str) -> tuple:
    """Move the live file (and any file a crashed replay left) aside and read it.

    Returns (entries, corrupt_count). A line that is not a JSON object with a `job`
    is appended verbatim to `.corrupt` instead of being replayed."""
    pending = dlq_path + ".replaying"
    if os.path.exists(dlq_path):
        with open(dlq_path, "rb") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            if os.path.exists(pending):
                with open(pending, "ab+") as out:
                    size = os.fstat(out.fileno()).st_size
                    if size and os.pread(out.fileno(), 1, size - 1) != b"\n":
                        out.write(b"\n")      # a crashed replay's torn tail
                    out.write(fh.read())
                os.remove(dlq_path)
            else:
                os.rename(dlq_path, pending)
    if not os.path.exists(pending):
        return [], 0
    entries, corrupt = [], []
    with open(pending, "rb") as fh:             # bytes: a torn multi-byte character must not abort the read
        for line in fh:
            if not line.strip():
                continue
            try:
                entry = json.loads(line.decode("utf-8"))
            except ValueError:                  # includes UnicodeDecodeError
                entry = None
            if isinstance(entry, dict) and isinstance(entry.get("job"), dict):
                entries.append(entry)
            else:
                corrupt.append(line if line.endswith(b"\n") else line + b"\n")
    if corrupt:
        fd = os.open(dlq_path + ".corrupt", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "ab") as out:
            os.fchmod(out.fileno(), 0o600)
            out.writelines(corrupt)
    return entries, len(corrupt)


def replay(dlq_path: str, send) -> dict:
    """Resend every line with `send(entry)`. Lines that still fail go back to the DLQ.

    `send` raises an exception carrying `status`, `code`, and `attempts` on failure.
    A 409 `idempotency_key_reuse` means CES already stored part of that turn under a
    different body, so the line is dropped and reported by its `key`. Raises ReplayInProgress if another
    replay of this DLQ is running."""
    os.makedirs(os.path.dirname(dlq_path) or ".", mode=0o700, exist_ok=True)
    fd = os.open(dlq_path + ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReplayInProgress(f"another replay of {dlq_path} is already running") from None
        return _replay_locked(dlq_path, send)


def _replay_locked(dlq_path: str, send) -> dict:
    entries, corrupt = _take(dlq_path)
    result = {"sent": 0, "kept": 0, "dropped": [], "corrupt": corrupt}
    if not entries:
        if os.path.exists(dlq_path + ".replaying"):
            os.remove(dlq_path + ".replaying")
        return result
    kept = []
    for entry in entries:
        try:
            send(entry)
            result["sent"] += 1
        except Exception as error:
            status, code = getattr(error, "status", None), getattr(error, "code", None)
            if status == 409 and code == "idempotency_key_reuse":
                result["dropped"].append(entry.get("key"))
                continue
            kept.append({**entry, "failed_at": time.time(), "status": status,
                         "error_code": code or type(error).__name__,
                         "attempts": entry.get("attempts", 0) + getattr(error, "attempts", 1)})
    if kept:
        append(dlq_path, kept)
    result["kept"] = len(kept)
    os.remove(dlq_path + ".replaying")
    return result
