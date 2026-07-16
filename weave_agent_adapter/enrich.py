"""Per-harness turn enrichment (profile `[enrich]` section).

Hooks carry the turn skeleton (prompts, tools, permissions) but no LLM-call
internals — no harness emits a hook per API call. Those live in the harness's
transcript, whose format is proprietary per harness. So enrichment is a named
strategy selected declaratively:

    [enrich]
    source = "claude-transcript"

An enricher runs once per turn at finalization and mutates the Turn (fills
`chat_calls`: model, token usage, finish reason, message text). A harness with
no `[enrich]` section, or an unreadable transcript, degrades gracefully — the
skeleton still emits.
"""
from __future__ import annotations

import datetime
import json

from .model import Session, Turn
from .redact import Redactor

_WINDOW_SLACK_S = 2.0        # transcript rows land within a couple seconds of hook times
_MAX_TEXT = 8000             # cap stored message text


def _epoch(iso: str):
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class ClaudeTranscriptEnricher:
    """Reads Claude Code's transcript JSONL and attaches one chat record per
    assistant API call inside the turn's time window. Subagent (sidechain)
    calls live in separate transcript files and are not read here."""

    def __init__(self, redactor: Redactor):
        self.redactor = redactor

    def enrich_turn(self, t: Turn, s: Session) -> None:
        if not s.transcript:
            return
        lo = t.started_at - _WINDOW_SLACK_S
        hi = (t.ended_at if t.ended_at is not None else t.started_at) + _WINDOW_SLACK_S
        prev_ts = None
        by_id: dict[str, dict] = {}
        no_id: list[dict] = []
        branch = None
        try:
            with open(s.transcript) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(r, dict):
                        continue
                    ts = _epoch(r.get("timestamp") or "")
                    if ts is not None and lo <= ts <= hi and r.get("gitBranch"):
                        branch = r["gitBranch"]           # last in-window value wins
                    if r.get("type") == "assistant" and not r.get("isSidechain") and ts is not None:
                        if lo <= ts <= hi:
                            msg = r.get("message") or {}
                            usage = msg.get("usage") or {}
                            if usage:
                                rec = self._record(msg, usage, prev_ts, ts, t)
                                mid = msg.get("id")
                                if mid:
                                    by_id[mid] = rec
                                else:
                                    no_id.append(rec)
                    if ts is not None:
                        prev_ts = ts
        except Exception:
            pass
        t.chat_calls.extend(no_id)
        t.chat_calls.extend(by_id.values())
        if branch:
            t.git_branch = branch

    def _record(self, msg, usage, prev_ts, ts, t: Turn) -> dict:
        content = [
            block for block in (msg.get("content") or [])
            if isinstance(block, dict)
        ]
        text = "\n".join(
            block.get("text", "")
            for block in content
            if block.get("type") == "text"
        ).strip() or None
        reasoning = "\n".join(
            block.get("thinking", "")
            for block in content
            if block.get("type") == "thinking"
        ).strip() or None
        tool_calls = [
            {
                "id": block.get("id") or "",
                "name": block.get("name") or "",
                "arguments": self.redactor.scrub(block.get("input") or {}),
            }
            for block in content
            if block.get("type") == "tool_use"
        ]
        if text:
            text = self.redactor.scrub(text)[:_MAX_TEXT]
        if reasoning:
            reasoning = self.redactor.scrub(reasoning)[:_MAX_TEXT]
        started = prev_ts if prev_ts is not None and prev_ts >= t.started_at else t.started_at
        return {
            "model": msg.get("model"),
            "provider_name": "anthropic",
            "response_id": msg.get("id"),
            "response_model": msg.get("model"),
            "started_at": min(started, ts),
            "ended_at": ts,
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": usage.get("reasoning_output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
            "finish_reason": msg.get("stop_reason"),
            "text": text,
            "reasoning": reasoning,
            "tool_calls": tool_calls,
        }


_ENRICHERS = {"claude-transcript": ClaudeTranscriptEnricher}


def make_enricher(profile_enrich: dict, redactor: Redactor):
    cls = _ENRICHERS.get((profile_enrich or {}).get("source"))
    return cls(redactor) if cls else None
