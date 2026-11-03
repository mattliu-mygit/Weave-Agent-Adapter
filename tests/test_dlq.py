"""Dead-letter file appends and replay."""
from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path

import pytest

from weave_agent_adapter import dlq
from weave_agent_adapter.ces import CesError


def _keys(path):
    with open(path) as fh:
        return [json.loads(line)["key"] for line in fh]


def test_append_creates_user_only_file(tmp_path):
    path = str(tmp_path / "state" / "dlq.jsonl")
    dlq.append(path, [dlq.record({"turns": []}, "k1", 503, "unavailable", 3)])

    assert os.stat(path).st_mode & 0o777 == 0o600
    assert _keys(path) == ["k1"]


def test_replay_sends_keeps_failures_and_drops_key_reuse(tmp_path):
    path = str(tmp_path / "dlq.jsonl")
    dlq.append(path, [dlq.record({"turns": []}, key, 503, "unavailable", 3) for key in ("ok", "reused", "down")])
    sent = []

    def send(entry):
        if entry["key"] == "reused":
            raise CesError(409, "idempotency_key_reuse", "stored under another body")
        if entry["key"] == "down":
            raise CesError(503, "unavailable", "still down")
        sent.append(entry["key"])

    assert dlq.replay(path, send) == {"sent": 1, "kept": 1, "dropped": ["reused"], "corrupt": 0}
    assert sent == ["ok"] and _keys(path) == ["down"]
    assert not os.path.exists(path + ".replaying")


def test_replay_of_missing_file_is_a_no_op(tmp_path):
    assert dlq.replay(str(tmp_path / "none.jsonl"), lambda e: None) == {"sent": 0, "kept": 0, "dropped": [], "corrupt": 0}


def test_append_during_replay_lands_in_the_new_file(tmp_path):
    path = str(tmp_path / "dlq.jsonl")
    dlq.append(path, [dlq.record({"turns": []}, "old", 503, "u", 1)])

    def send(entry):
        dlq.append(path, [dlq.record({"turns": []}, "new", 503, "u", 1)])   # the sidecar, meanwhile

    dlq.replay(path, send)
    assert _keys(path) == ["new"]


def test_replay_refuses_while_another_replay_holds_the_lock(tmp_path):
    path = str(tmp_path / "dlq.jsonl")
    dlq.append(path, [dlq.record({"turns": []}, "k1", 503, "u", 1)])
    before = Path(path).read_text()
    sent = []

    with open(path + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)              # the other replay
        with pytest.raises(dlq.ReplayInProgress):
            dlq.replay(path, sent.append)

    assert sent == []
    assert Path(path).read_text() == before
    assert not os.path.exists(path + ".replaying")


def test_replay_sets_aside_a_torn_line_instead_of_failing(tmp_path):
    path = str(tmp_path / "dlq.jsonl")
    dlq.append(path, [dlq.record({"turns": []}, "good", 503, "u", 1)])
    with open(path, "a") as fh:
        fh.write('{"v": 1, "path"')                   # the sidecar died mid-write
    sent = []

    result = dlq.replay(path, lambda entry: sent.append(entry["key"]))

    assert result == {"sent": 1, "kept": 0, "dropped": [], "corrupt": 1}
    assert sent == ["good"]
    assert not os.path.exists(path + ".replaying")
    assert Path(path + ".corrupt").read_text().strip() == '{"v": 1, "path"'
    assert os.stat(path + ".corrupt").st_mode & 0o777 == 0o600


def test_replay_sets_aside_valid_json_that_is_not_an_entry(tmp_path):
    path = str(tmp_path / "dlq.jsonl")
    with open(path, "w") as fh:
        fh.write('[1, 2]\n{"no_job": true}\n{"key": "ok", "job": {}}\n')

    result = dlq.replay(path, lambda entry: None)

    assert result == {"sent": 1, "kept": 0, "dropped": [], "corrupt": 2}
    assert Path(path + ".corrupt").read_text() == '[1, 2]\n{"no_job": true}\n'


def test_append_blocked_on_the_lock_rechecks_the_file_after_replay_moves_it(tmp_path):
    path = str(tmp_path / "dlq.jsonl")
    dlq.append(path, [dlq.record({"turns": []}, "old", 503, "u", 1)])

    with open(path) as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        writer = threading.Thread(target=dlq.append, args=(path, [dlq.record({"turns": []}, "late", 503, "u", 1)]))
        writer.start()
        writer.join(timeout=0.3)
        assert writer.is_alive()                      # blocked on the flock
        os.rename(path, path + ".moved")              # replay moves the file aside
        fcntl.flock(holder, fcntl.LOCK_UN)
    writer.join(timeout=5)

    assert not writer.is_alive()
    assert _keys(path) == ["late"]
    assert _keys(path + ".moved") == ["old"]


def test_replay_resends_a_file_left_by_a_crashed_replay_together_with_the_live_file(tmp_path):
    path = str(tmp_path / "dlq.jsonl")
    dlq.append(path + ".replaying", [dlq.record({"turns": []}, "stranded", 503, "u", 1)])
    dlq.append(path, [dlq.record({"turns": []}, "live", 503, "u", 1)])
    sent = []

    result = dlq.replay(path, lambda entry: sent.append(entry["key"]))

    assert result == {"sent": 2, "kept": 0, "dropped": [], "corrupt": 0}
    assert sorted(sent) == ["live", "stranded"]
    assert not os.path.exists(path) and not os.path.exists(path + ".replaying")


def test_replay_sets_aside_a_line_torn_inside_a_multibyte_character(tmp_path):
    path = str(tmp_path / "dlq.jsonl")
    torn = b'{"key": "x", "job": {"text": "caf\xc3'
    Path(path).write_bytes(b'{"v": 2, "key": "ok", "job": {}}\n' + torn)
    sent = []

    result = dlq.replay(path, lambda entry: sent.append(entry["key"]))

    assert result == {"sent": 1, "kept": 0, "dropped": [], "corrupt": 1}
    assert sent == ["ok"]
    assert not os.path.exists(path + ".replaying")
    assert Path(path + ".corrupt").read_bytes() == torn + b"\n"


def test_append_after_a_torn_tail_starts_a_new_line(tmp_path):
    path = str(tmp_path / "dlq.jsonl")
    Path(path).write_text('{"v": 1, "path"')           # torn tail, no newline
    dlq.append(path, [dlq.record({"turns": []}, "good", 503, "u", 1)])
    sent = []

    result = dlq.replay(path, lambda entry: sent.append(entry["key"]))

    assert result == {"sent": 1, "kept": 0, "dropped": [], "corrupt": 1}
    assert sent == ["good"]


def test_merge_of_a_crashed_replays_torn_tail_does_not_glue_the_live_file_onto_it(tmp_path):
    path = str(tmp_path / "dlq.jsonl")
    Path(path + ".replaying").write_text('{"v": 1, "path"')
    dlq.append(path, [dlq.record({"turns": []}, "live", 503, "u", 1)])
    sent = []

    result = dlq.replay(path, lambda entry: sent.append(entry["key"]))

    assert result == {"sent": 1, "kept": 0, "dropped": [], "corrupt": 1}
    assert sent == ["live"]
