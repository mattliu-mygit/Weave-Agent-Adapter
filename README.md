# weave-agent-adapter

See what your coding agents did and what you allowed them to do.
`weave-agent-adapter` records agent turns as typed W&B Weave conversations and
spans for model calls, tools, subagents, approvals, rejections, and mid-turn
steering. These spans can be viewed in Agents and used for Signals.

It attaches through external command hooks: no harness source changes, no
in-process SDK, and no synchronous network work on the hook path. Claude Code,
Codex, and Gemini CLI profiles ship with the package; another JSON-on-stdin
command-hook harness can be added with one TOML profile.

## Span shape

```text
invoke_agent claude-code  "add logging" -> "done"
├── chat claude-opus-4    1.2k input / 80 output tokens
├── execute_tool Bash     approved
├── execute_tool Edit     rejected: "use the logger"
├── invoke_agent Explore   agent_id=a1
└── execute_tool Read     agent_id=a1
```

Each finalized turn is logged once with `weave.log_turn` as a typed Turn span
in its own trace. Stable thread identifiers connect turns and forked/resumed
sessions into Weave conversations. The public batch API uses flat typed child
spans; subagent tool results retain their observed agent ID.

## Install

Install the sidecar dependencies:

```bash
pip install "weave-agent-adapter[sidecar]"
```

Provide W&B credentials to the sidecar, or use an existing authenticated
environment:

```bash
export WANDB_API_KEY="..."
```

Set the target in `~/.weave-agent-adapter/config.toml`:

```toml
[weave]
project = "my-entity/my-project"
```

Register the hooks:

```bash
weave-agent-adapter install                       # Claude Code
weave-agent-adapter install --harness codex       # Codex
weave-agent-adapter install --harness gemini-cli  # Gemini CLI
```

Codex skips new or changed non-managed hooks until you review and trust their
exact definitions. After installing Codex hooks, open `/hooks` in Codex and
trust the adapter entries.

Gemini CLI captures sessions, turns, model selection, tools, final responses,
and compaction through its stable non-streaming hooks. Permissions, subagents,
transcript enrichment, and configuration fingerprinting are omitted because
that profile has no reliable source for them; the rest of the trace still
emits normally. Claude Code's `PermissionDenied` hook covers auto-mode
classifier denials, not denials made in the manual permission dialog.

The command atomically merges adapter entries into the harness settings and
preserves existing configuration. Remove only the adapter entries with:

```bash
weave-agent-adapter uninstall [--harness codex|gemini-cli]
```

Use `--local` for repository-scoped hook settings.

## Runtime

Hooks perform a size- and time-bounded send to a user-only Unix socket and
always exit zero without making permission decisions. They use empty stdout by
default and may return an empty JSON object when the harness requires a JSON
acknowledgment. The first event starts a singleton sidecar, which normalizes
events, redacts values, builds the turn, and maps it to public Weave
Conversation SDK objects. It exits when idle and restarts on demand.

A turn-end hook hands the completed turn to the emitter immediately and only
once. The Weave SDK owns agent-span routing, asynchronous export, batching, and
network retry, so the hook remains fire-and-forget without a second adapter
queue. Delivery is deliberately best-effort: there is no raw capture, spool,
outbox, replay, or second tracing plane. Local diagnostics contain metadata
only, never payload values or exception messages.

The turn root contains the user prompt and mid-turn steering. Assistant text
is emitted as typed LLM child output so it renders as an assistant message in
Weave Conversations and remains usable by Signals. Codex also opts into
best-effort transcript enrichment for intermediate assistant messages, public
reasoning summaries, model-call usage, and model names. If those native details
are unavailable, the final turn-end reply is still emitted as a fallback LLM
child.

## Configuration

Environment variables override `~/.weave-agent-adapter/config.toml`.

```toml
[weave]
project = "my-entity/my-project"  # or a bare project using W&B's default entity
project_per_repo = false          # route to entity/<cwd-leaf> when true

[redaction]
enabled = true
redact_keys = ["api_key", "authorization", "token", "password"]

[sampling]
session_rate = 1.0

[sidecar]
idle_shutdown_s = 120
session_ttl_s = 3600
```

For a bare project, `weave.init` resolves W&B's authenticated default entity.
Weave reads `WANDB_API_KEY` or existing W&B credentials; secrets never belong
in adapter configuration. `WEAVE_AGENT_ADAPTER_DISABLE=1` disables hook
forwarding.

## Add a harness

Copy one of the profiles in
[`weave_agent_adapter/profiles/`](weave_agent_adapter/profiles/), then declare:

- native event to canonical action mappings;
- dotted JSON payload fields;
- optional thread, transcript-enrichment, and configuration-surface behavior;
- user/local settings paths and the events to register.

Common fields may be supplemented by event-specific field paths. Missing
lifecycle events and optional metadata degrade by omission. See the
[harness profile contract](specs/HARNESS_PROFILES.md) for the supported shape.

## Design contracts

- [Product architecture and invariants](specs/DESIGN.md)
- [Harness profile contract](specs/HARNESS_PROFILES.md)
- [Weave agent span contract](specs/WEAVE_SPAN_CONTRACT.md)

## Development

```bash
pip install -e ".[sidecar,dev]"
pytest -q
```

The suite covers reducer correlation and lifecycle, every registered hook,
installer safety, privacy, project routing, typed span mapping, and SDK setup.

## License

See [LICENSE](LICENSE).
