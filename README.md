# weave-agent-adapter

Trace agent-harness sessions to [Weights & Biases Weave](https://wandb.ai/site/weave) as a nested trace you can inspect, filter, and analyze. Two harnesses ship as profiles: [Claude Code](https://docs.claude.com/en/docs/claude-code) and [Codex](https://developers.openai.com/codex). Any harness (open source, closed source, personal, etc.) with a hook-like system can be added with a profile and no code (see [Bring your own harness](#bring-your-own-harness)).

## What it captures

A session becomes one Weave trace, nested to match what actually happened:

```
session
└── turn                     "add logging to auth.py"
    ├── input                the prompt
    ├── tool:Bash            approved (auto)
    ├── tool:Edit            rejected, deny "use the logger, not print"
    ├── steering             user redirect mid-turn
    └── tool:Edit            approved (user)
```

Beyond the call tree, it records the human-in-the-loop signals: approval, rejection, and steering.

## Quickstart

Install (the `sidecar` extra pulls in Weave; the hook itself is stdlib-only):

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

- Hooks (external one-line commands, auto-registered) emit each event to a local socket and exit immediately.
- A sidecar, spawned at session start as one warm process per machine, hosts a single `weave.init()` client and turns events into nested Weave calls. A long-lived process is what lets the SDK's async batching and retry apply, and keeps spans in Weave's native call model rather than raw OTel JSON.

Adopters write zero lines of code: installing the hooks registers everything.

## Bring your own harness

The core runs on a fixed set of canonical actions (session, turn, tool, permission, subagent, compaction). Each harness plugs in through a declarative TOML profile that maps its hook events and payload fields onto those actions, so tracing a new harness needs a profile and no code. The only requirement is that the harness can run a command per lifecycle event and hand it the event payload as JSON on stdin.

To add one, copy a shipped profile as a template ([claude-code.toml](weave_agent_adapter/profiles/claude-code.toml) or [codex.toml](weave_agent_adapter/profiles/codex.toml)) to `weave_agent_adapter/profiles/<name>.toml` and edit the tables:

```toml
[harness]
name      = "myharness"
adapter   = "command-hook"      # the built-in command dispatcher
transport = "stdin-json"        # payload delivered as JSON on stdin (the supported mode)

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

[registration]                  # how `install` wires the hooks
kind    = "codex-hooks"         # a supported target (see below)
command = "weave-agent-adapter hook --harness myharness"
events  = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
```

Map only the events your harness emits. Missing ones degrade gracefully: a harness with no session-end event closes sessions via the idle sweep, and one with no pre-tool event synthesizes the span from the completion.

`kind` selects where `install` writes the hook registration. Two targets ship: `claude-code-settings` (`~/.claude/settings.json`) and `codex-hooks` (`~/.codex/hooks.json`). If your harness reads one of those formats, reuse its kind; otherwise add one line to the target map in `install.py`, or skip `install` and wire the printed per-event command into your harness by hand.

## License

See [LICENSE](LICENSE).
