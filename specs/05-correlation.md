# Spec 05: Tool and subagent correlation

A stable harness-provided `tool_use_id` is authoritative across pre, permission,
completion, and failure events. A supplied ID that is unknown never falls back
to another running tool.

For harnesses or events without an ID, the reducer considers running tools with
the same tool name, then compatible redacted input when input is present. It
reuses a tool only when exactly one candidate remains. No match or multiple
matches produce a separate completion-only tool record rather than mutating an
arbitrary parallel call.

Repeated close events do not change a tool that is already terminal. A denied
permission closes its matched tool as rejected. A completion implies approval
only for the matched running tool.

Subagents correlate strictly by `agent_id`. An interior tool can materialize a
missing subagent-start record, but a stop without the tracked ID cannot close a
different subagent. Late subagent events remain part of the pending turn, and
the emitted root timestamp expands to contain them.
