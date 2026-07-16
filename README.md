# weave-agent-adapter

See exactly what your coding agents did, and what you let them do. weave-agent-adapter records every agent session to [Weights & Biases Weave](https://wandb.ai/site/weave) as a nested trace of turns, tool calls, and the human-in-the-loop moments other tools drop on the floor: approvals, rejections, and mid-turn steering.

Point it at **any** harness with a hook-like system, closed source, open source, or one you wrote yourself, through a **single config file**. No code to write, no forking the harness (see [Bring your own harness](#bring-your-own-harness)). [Claude Code](https://docs.claude.com/en/docs/claude-code) and [Codex](https://developers.openai.com/codex) ship built in.

## What it captures

Each turn becomes one trace, stitched into its conversation, nested to match what actually happened:

```
invoke_agent claude-code     "add logging to auth.py" -> "done, wired the logger"
├── chat claude-opus-4       1.2k in / 80 out tokens
├── execute_tool Bash        approved (auto)
├── execute_tool Edit        rejected, deny "use the logger, not print"
├── invoke_agent Explore     subagent, its tools nested inside
└── execute_tool Edit        approved (user)
```

Beyond the trace tree, it records the human-in-the-loop signals — approval, rejection, and mid-turn steering — and links forked/resumed sessions into one conversation.

## Quickstart

Install (the `sidecar` extra pulls in wandb + OpenTelemetry; the hook itself is stdlib-only):

```bash
pip install "weave-agent-adapter[sidecar]"
```

Authenticate with W&B once. This stores your key in `~/.netrc`, so there is nothing to export per shell:

```bash
wandb login
```

Set your Weave project in `~/.weave-agent-adapter/config.toml` (or the `WEAVE_PROJECT` env var):

```toml
[weave]
project = "my-entity/my-project"
```

Register the hooks for your harness. This is idempotent and removable:

```bash
weave-agent-adapter install                     # Claude Code (default)
weave-agent-adapter install --harness codex      # Codex
```

Now use your agent normally. Each session appears in Weave as a nested trace. The sidecar starts on the first event and scales to zero when idle. To remove the hooks:

```bash
weave-agent-adapter uninstall [--harness codex]
```

## How it works

The harness is never modified.

- Hooks (external one-line commands, auto-registered) perform a time- and size-bounded send to a local socket and exit without making permission decisions.
- A sidecar reduces the event stream into turns (with their tools, subagents, permissions, and steering) and emits each finalized turn as one OTel GenAI trace: `invoke_agent` root, `execute_tool` children, nested subagents. It uses Weave's turn/thread attributes so those traces appear in Conversations.
- The standard batched OTLP/HTTP exporter sends spans directly to `https://trace.wandb.ai/otel/v1/traces`. Delivery is best-effort; local payload-free diagnostics make authentication, initialization, and flush failures visible.

Adopters write zero lines of code: installing the hooks registers everything.

## Bring your own harness

The core runs on a fixed set of canonical actions (session, turn, tool, permission, subagent, compaction). Each harness plugs in through a declarative TOML profile that maps its hook events and payload fields onto those actions, so tracing a new harness needs a profile and no code. The only requirement is that the harness can run a command per lifecycle event and hand it the event payload as JSON on stdin.

To add one, copy a shipped profile as a template ([claude-code.toml](weave_agent_adapter/profiles/claude-code.toml) or [codex.toml](weave_agent_adapter/profiles/codex.toml)) to `weave_agent_adapter/profiles/<name>.toml` and edit the tables:

```toml
[harness]
name    = "myharness"
adapter = "command-hook"        # runs a command per event with the payload as JSON on stdin

[events]                        # native hook event -> canonical action
SessionStart      = "session_start"
UserPromptSubmit  = "turn_start"
PreToolUse        = "tool_pre"
PostToolUse       = "tool_post"
PermissionRequest = "permission_request"
SubagentStop      = "subagent_stop"
Stop              = "turn_end"
SessionEnd        = "session_end"

[fields]                        # canonical field -> dotted path in the payload
session_id  = "session_id"
tool_name   = "tool_name"
tool_input  = "tool_input"
tool_output = "tool_response"
tool_use_id = "tool_use_id"     # per-tool-call correlation id, if the harness has one
cwd         = "cwd"

[enrich]                        # optional; LLM-call internals (chat spans, tokens) from the harness's transcript
source = "claude-transcript"    # named strategy; omit if the harness has no transcript

[thread]                        # optional; how to link forked/resumed sessions into one thread
source   = "field"              # "field" | "transcript_root" | omit for none
id_field = "conversation_id"    # for source="field": a [fields] name holding a stable conversation id
# for source="transcript_root": read the transcript's first message id
# skip_field = "isSidechain"    #   rows to skip when finding the root
# id_key     = "uuid"           #   the row field to use as the id

[registration]                  # where and how `install` wires the hooks
user_path  = "~/.myharness/hooks.json"   # the harness's hook settings file
local_path = ".myharness/hooks.json"     # project-scoped variant (install --local)
command    = "weave-agent-adapter hook --harness myharness"
events     = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
```

Map only the events your harness emits. Missing ones — and the optional `[enrich]`/`[thread]` sections — degrade gracefully: no session-end event → sessions finalize via the idle sweep; no pre-tool event → the tool is synthesized from the completion; no `[enrich]` → no chat spans/tokens; no `[thread]` → sessions aren't linked into conversations. Nothing is Claude-specific in the core: tool names and thread derivation are declared here, not hardcoded.

`install` merges the hooks into the file named by `user_path` (or `local_path` with `--local`), preserving any other keys already there; `uninstall` removes only our entries. Any harness whose hook file uses the standard `{"hooks": {event: [...]}}` shape (Claude Code's `settings.json`, Codex's `hooks.json`, and most command-hook systems) works with no installer code changes.

## License

See [LICENSE](LICENSE).
