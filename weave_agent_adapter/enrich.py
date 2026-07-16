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


class CodexTranscriptEnricher:
    """Attach Codex rollout model cycles for one observed turn.

    Codex identifies turn boundaries explicitly in its JSONL rollout. Each
    token-count event closes one model cycle; assistant output and public
    reasoning summaries observed since the previous count belong to that
    cycle. Native tool-call rows are intentionally ignored because hooks
    already provide the authoritative typed Tool spans.
    """

    def __init__(self, redactor: Redactor):
        self.redactor = redactor

    def enrich_turn(self, t: Turn, s: Session) -> None:
        if not s.transcript or not t.turn_id:
            return
        active = False
        model = t.model
        cycle_start = t.started_at
        texts: list[str] = []
        reasoning: list[str] = []
        response_id = None
        finish_reason = None

        def finish_cycle(ended_at, usage=None):
            nonlocal cycle_start, texts, reasoning, response_id, finish_reason
            usage = usage or {}
            if not texts and not reasoning and not usage:
                cycle_start = ended_at
                return
            text = "\n\n".join(texts).strip() or None
            thought = "\n".join(reasoning).strip() or None
            if text:
                text = self.redactor.scrub(text)[:_MAX_TEXT]
            if thought:
                thought = self.redactor.scrub(thought)[:_MAX_TEXT]
            t.chat_calls.append({
                "model": model,
                "provider_name": "openai",
                "response_id": response_id,
                "response_model": model,
                "started_at": min(cycle_start, ended_at),
                "ended_at": ended_at,
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "reasoning_tokens": usage.get("reasoning_output_tokens"),
                "cache_read_tokens": usage.get("cached_input_tokens"),
                "cache_creation_tokens": None,
                "finish_reason": finish_reason,
                "text": text,
                "reasoning": thought,
                "tool_calls": [],
            })
            cycle_start = ended_at
            texts = []
            reasoning = []
            response_id = None
            finish_reason = None

        try:
            with open(s.transcript) as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    payload = row.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    row_type = row.get("type")
                    event_type = payload.get("type")
                    timestamp = _epoch(row.get("timestamp") or "")

                    if row_type == "event_msg" and event_type == "task_started":
                        if active:
                            break
                        active = payload.get("turn_id") == t.turn_id
                        if active and timestamp is not None:
                            cycle_start = max(t.started_at, timestamp)
                        continue
                    if not active:
                        continue

                    if row_type == "turn_context" and payload.get("turn_id") == t.turn_id:
                        model = payload.get("model") or model
                    elif row_type == "response_item" and event_type == "reasoning":
                        reasoning.extend(
                            str(item.get("text"))
                            for item in payload.get("summary") or []
                            if isinstance(item, dict) and item.get("text")
                        )
                        response_id = payload.get("id") or response_id
                    elif (row_type == "response_item" and event_type == "message"
                          and payload.get("role") == "assistant"):
                        texts.extend(
                            str(item.get("text"))
                            for item in payload.get("content") or []
                            if isinstance(item, dict)
                            and item.get("type") == "output_text"
                            and item.get("text")
                        )
                        response_id = payload.get("id") or response_id
                        if payload.get("phase") == "final_answer":
                            finish_reason = "stop"
                    elif row_type == "event_msg" and event_type == "token_count":
                        info = payload.get("info") or {}
                        usage = info.get("last_token_usage") or {}
                        finish_cycle(timestamp or cycle_start, usage)
                    elif (row_type == "event_msg" and event_type == "task_complete"
                          and payload.get("turn_id") == t.turn_id):
                        if t.output_text is None and payload.get("last_agent_message") is not None:
                            t.output_text = self.redactor.scrub(payload["last_agent_message"])
                        finish_cycle(timestamp or cycle_start)
                        break
        except Exception:
            return


_ENRICHERS = {
    "claude-transcript": ClaudeTranscriptEnricher,
    "codex-transcript": CodexTranscriptEnricher,
}


def make_enricher(profile_enrich: dict, redactor: Redactor):
    cls = _ENRICHERS.get((profile_enrich or {}).get("source"))
    return cls(redactor) if cls else None
