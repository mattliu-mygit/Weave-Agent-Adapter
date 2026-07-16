"""Map finalized harness-neutral turns to Weave's public Conversation SDK."""
from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone

from . import __version__
from .diagnostics import diagnose
from .model import Session, ToolStatus, Turn

NS = "weave_agent_adapter"
_MAX_TOOL_OUTPUT = 32_000
_FLUSH_TIMEOUT_MS = 5_000


def _utc(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _window(start: float, end: float, low: float, high: float) -> tuple[datetime, datetime]:
    bounded_start = min(max(start, low), high)
    bounded_end = min(max(end, bounded_start), high)
    return _utc(bounded_start), _utc(bounded_end)


def _bounded(value):
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str,
    )
    if len(text) <= _MAX_TOOL_OUTPUT:
        return value
    marker = "…[truncated]"
    return text[:_MAX_TOOL_OUTPUT - len(marker)] + marker


def serializable_payload(value):
    """Turn SDK objects into structured JSON-compatible debug values."""
    if hasattr(value, "model_dump"):
        return serializable_payload(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {key: serializable_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable_payload(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


class WeaveTurnEmitter:
    """Best-effort one-shot handoff from the domain model to `weave.log_turn`."""

    def __init__(self, weave_module=None, *, emit=None):
        self._weave_module = weave_module
        self._emit = emit
        self._project = None

    def _weave(self):
        if self._weave_module is None:
            self._weave_module = importlib.import_module("weave")
        return self._weave_module

    @staticmethod
    def _conversation():
        return importlib.import_module("weave.conversation")

    def emit_turn(self, turn: Turn, session: Session) -> bool:
        try:
            payload = self._build_turn(turn, session)
            if self._emit is not None:
                return self._emit(payload, session.project) is not False
            sdk = self._weave()
            if self._project != session.project:
                sdk.init(session.project)
                self._project = session.project
            sdk.log_turn(**payload)
            return True
        except Exception as exc:
            diagnose("export", project=session.project, error=exc)
            return False

    def flush(self) -> bool:
        try:
            from opentelemetry import trace

            force_flush = getattr(trace.get_tracer_provider(), "force_flush", None)
            if force_flush is None:
                return True
            result = force_flush(timeout_millis=_FLUSH_TIMEOUT_MS)
            if result is False:
                diagnose("export_flush")
                return False
            return True
        except Exception as exc:
            diagnose("export_flush", error=exc)
            return False

    def _build_turn(self, turn: Turn, session: Session) -> dict:
        types = self._conversation()
        chat_calls = list(turn.chat_calls)
        output_text = str(turn.output_text).strip() if turn.output_text is not None else ""
        has_final_output = any(
            (chat_text := str(chat.get("text") or "").strip()) == output_text
            or chat_text.startswith(output_text + "\n")
            or chat_text.endswith("\n" + output_text)
            for chat in chat_calls
        )
        if output_text and not has_final_output:
            at = turn.ended_at or turn.started_at
            fallback_model = turn.model or next(
                (chat.get("model") for chat in reversed(chat_calls) if chat.get("model")),
                "",
            )
            chat_calls.append({
                "model": fallback_model,
                "provider_name": "",
                "started_at": at,
                "ended_at": at,
                "finish_reason": "stop",
                "text": output_text,
                "tool_calls": [],
            })
        child_ends = [
            *(call.ended_at or call.started_at for call in turn.tool_calls.values()),
            *(record.get("ended_at") or record["started_at"]
              for record in turn.subagents.values()),
            *(chat["ended_at"] for chat in chat_calls),
        ]
        root_end = max([turn.ended_at or turn.started_at, *child_ends])

        messages = []
        if turn.input_text is not None:
            messages.append(types.Message.user(str(turn.input_text)))
        messages.extend(
            types.Message.user(str(item.text))
            for item in turn.steering
            if item.text is not None
        )

        spans = [
            *(self._llm(chat, turn.started_at, root_end, types)
              for chat in chat_calls),
            *(self._tool(call, turn.started_at, root_end, types)
              for call in turn.tool_calls.values()),
            *(self._subagent(record, turn.started_at, root_end, types)
              for record in turn.subagents.values()),
        ]
        spans.sort(key=lambda span: span.started_at or _utc(turn.started_at))

        attributes = {
            f"{NS}.integration": "weave-agent-adapter",
            f"{NS}.version": __version__,
            f"{NS}.harness": session.harness or "agent",
            f"{NS}.session_id": str(session.session_id),
            f"{NS}.incomplete": bool(turn.incomplete),
            f"{NS}.steering_count": len(turn.steering),
            f"{NS}.denial_count": sum(
                call.status == ToolStatus.REJECTED for call in turn.tool_calls.values()
            ),
            f"{NS}.tool_error_count": sum(
                call.status == ToolStatus.ERROR for call in turn.tool_calls.values()
            ),
            f"{NS}.compaction_count": len(turn.compactions),
        }
        if session.cwd:
            attributes[f"{NS}.cwd"] = str(session.cwd)
        if session.config_version:
            attributes[f"{NS}.config_version"] = session.config_version
        if turn.git_branch:
            attributes[f"{NS}.git_branch"] = str(turn.git_branch)
        if turn.effort_level:
            attributes[f"{NS}.effort_level"] = str(turn.effort_level)
        if turn.permission_mode:
            attributes[f"{NS}.permission_mode"] = str(turn.permission_mode)
        if turn.turn_id:
            attributes[f"{NS}.turn_id"] = str(turn.turn_id)
        if turn.compactions:
            attributes[f"{NS}.compaction_triggers"] = [
                str(trigger or "unknown") for _, trigger in turn.compactions
            ]

        model = next(
            (chat.get("model") for chat in reversed(chat_calls) if chat.get("model")),
            turn.model or "",
        )
        return {
            "conversation_id": str(session.thread_id or session.session_id),
            "agent_name": session.harness or "agent",
            "model": model,
            "messages": messages,
            "spans": spans,
            "started_at": _utc(turn.started_at),
            "ended_at": _utc(root_end),
            "attributes": attributes,
        }

    @staticmethod
    def _llm(chat: dict, low: float, high: float, types):
        parts = []
        if chat.get("text"):
            parts.append(types.TextPart(content=str(chat["text"])))
        parts.extend(
            types.ToolCallPart(
                id=str(call.get("id") or ""),
                name=str(call.get("name") or ""),
                arguments=call.get("arguments") or {},
            )
            for call in chat.get("tool_calls") or []
        )
        output_messages = [types.Message(role="assistant", parts=parts)] if parts else []
        started_at, ended_at = _window(
            chat["started_at"], chat["ended_at"], low, high,
        )
        return types.LLM(
            model=chat.get("model") or "",
            provider_name=chat.get("provider_name") or "",
            response_id=chat.get("response_id") or "",
            response_model=chat.get("response_model") or chat.get("model") or "",
            usage=types.Usage(
                input_tokens=int(chat.get("input_tokens") or 0),
                output_tokens=int(chat.get("output_tokens") or 0),
                reasoning_tokens=int(chat.get("reasoning_tokens") or 0),
                cache_creation_input_tokens=int(chat.get("cache_creation_tokens") or 0),
                cache_read_input_tokens=int(chat.get("cache_read_tokens") or 0),
            ),
            reasoning=types.Reasoning(content=chat.get("reasoning") or ""),
            finish_reasons=[chat["finish_reason"]] if chat.get("finish_reason") else [],
            input_messages=[],
            output_messages=output_messages,
            started_at=started_at,
            ended_at=ended_at,
        )

    @staticmethod
    def _tool(call, low: float, high: float, types):
        started_at, ended_at = _window(
            call.started_at, call.ended_at or call.started_at, low, high,
        )
        permission = None
        if call.permission:
            permission = {
                "decision": call.permission.decision.value,
                "reason": call.permission.reason,
            }
        return types.Tool(
            name=call.tool_name,
            arguments=call.tool_input,
            result={
                "status": call.status.value,
                "agent_id": call.agent_id,
                "output": _bounded(call.output),
                "error": _bounded(call.error),
                "permission": permission,
            },
            tool_call_id=call.correlation_key,
            tool_type="function",
            started_at=started_at,
            ended_at=ended_at,
        )

    @staticmethod
    def _subagent(record: dict, low: float, high: float, types):
        started_at, ended_at = _window(
            record["started_at"], record.get("ended_at") or record["started_at"],
            low, high,
        )
        return types.SubAgent(
            name=record["type"],
            agent_id=str(record.get("agent_id") or ""),
            started_at=started_at,
            ended_at=ended_at,
        )
