# Spec 06: Weave OTel mapping

Each finalized turn is one OTel trace sent through the protobuf OTLP/HTTP
exporter. The root span is `invoke_agent <harness>`. Direct tools are
`execute_tool <name>` children; subagents are nested `invoke_agent <type>` spans
with their tools underneath; transcript-enriched LLM calls are `chat <model>`.

Root attributes include:

- `gen_ai.operation.name=invoke_agent`
- `gen_ai.agent.name`
- `gen_ai.conversation.id`
- `wandb.thread_id` with the same fork-stable identifier
- `wandb.is_turn=true`
- `input.value` and `output.value` when present
- prompt/completion GenAI attributes
- adapter session, friction-counter, configuration, branch, and effort fields

Tool spans include operation/name/call ID, redacted arguments and result,
adapter status, and permission decision/reason. Failed tools carry their
redacted error as the result and OTel error status. Steering and compaction are
span events.

Hook `captured_at` values are authoritative. A root end is extended to contain
all child ends, including subagents completing after the harness Stop event.

The default endpoint is `https://trace.wandb.ai/otel/v1/traces`. Authentication
uses the `wandb-api-key` header. `wandb.entity` and `wandb.project` are OTel
resource attributes. The Weave SDK calls API is not used.
