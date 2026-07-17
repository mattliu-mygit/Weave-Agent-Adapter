# weave-agent-adapter

See what your coding agents did and what you allowed them to do.
`weave-agent-adapter` records agent turns as typed W&B Weave conversations and
spans for model calls, tools, subagents, approvals, rejections, and mid-turn
steering. View the resulting traces in Weave Agents or use them with Signals.

It attaches through external command hooks: no harness source changes, no
in-process SDK, and no synchronous network work on the hook path. Claude Code,
Codex, and Gemini CLI profiles ship with the package.

## What gets recorded

```text
invoke_agent claude-code  "add logging" -> "done"
├── chat claude-opus-4    1.2k input / 80 output tokens
├── execute_tool Bash     approved
├── execute_tool Edit     rejected: "use the logger"
├── invoke_agent Explore  agent_id=a1
└── execute_tool Read     agent_id=a1
```

Each completed turn is logged once with `weave.log_turn` as a typed Turn span
in its own trace. Stable thread identifiers connect turns and forked or resumed
sessions into Weave conversations.

## Quickstart

### Prerequisites

You need:

- Python 3.10 or newer;
- a [Weights & Biases account](https://wandb.ai/);
- Claude Code, Codex, or Gemini CLI.

The examples below use a POSIX shell on macOS or Linux.

### 1. Install

Choose either the published package or a source checkout. Both use a virtual
environment so the adapter and its sidecar dependencies stay isolated.

#### From PyPI

```bash
python3 -m venv ~/.venvs/weave-agent-adapter
source ~/.venvs/weave-agent-adapter/bin/activate
python -m pip install --upgrade pip
python -m pip install "weave-agent-adapter[sidecar]"
```

If the first release is not yet available from your package index, use the
source installation below.

#### From source

```bash
git clone https://github.com/mattliu-mygit/Weave-Agent-Adapter.git
cd Weave-Agent-Adapter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[sidecar]"
```

Keep the environment active through hook registration. The installer records
the adapter executable's absolute path, so the hooks keep working after you
deactivate the environment. Reinstall the hooks if you later move or recreate
that environment.

### 2. Authenticate with W&B

Use an existing authenticated W&B environment or export an API key before
starting your coding harness:

```bash
export WANDB_API_KEY="..."
```

If you already use the W&B CLI, credentials saved by `wandb login` work too.
Never put the API key in the adapter configuration file.

### 3. Choose a Weave project

Create the configuration directory:

```bash
mkdir -p ~/.weave-agent-adapter
```

Then create `~/.weave-agent-adapter/config.toml`:

```toml
[weave]
project = "my-entity/my-project"
```

Replace both names with your W&B entity and project. A bare project name also
works and uses your authenticated default entity.

### 4. Register one harness

Run the command for the harness you use:

```bash
weave-agent-adapter install                         # Claude Code
weave-agent-adapter install --harness codex         # Codex
weave-agent-adapter install --harness gemini-cli    # Gemini CLI
```

The installer merges adapter entries into the harness settings atomically and
preserves existing hooks. Add `--local` to write repository-scoped settings
instead of user-wide settings.

Codex skips new or changed non-managed hooks until you trust their exact
definitions. In a Codex surface with slash commands, run `/hooks`, review the
adapter entries, and trust them. If `/hooks` is unavailable, review the
generated `~/.codex/hooks.json`; approve the hooks when Codex presents its
trust prompt.

### 5. Verify the installation

1. Start a new session in the registered harness.
2. Complete one normal prompt and response.
3. Open the configured project in W&B Weave and look for the new agent trace.

Export is asynchronous and best-effort, so a trace may take a moment to appear.
On a healthy run the diagnostic log may be empty. If no trace arrives, inspect
the metadata-only log:

```bash
tail -n 50 ~/.weave-agent-adapter/adapter.log
```

While the sidecar is active, its user-only socket and singleton lock live at:

```text
~/.weave-agent-adapter/sidecar.sock
~/.weave-agent-adapter/sidecar.sock.lock
```

The socket disappears when the sidecar shuts down after its idle timeout. If
you authenticated with `WANDB_API_KEY`, make sure the variable is present in
the environment that launches the coding harness.

## Uninstall

Remove only the adapter's hook entries with the command for your harness:

```bash
weave-agent-adapter uninstall                         # Claude Code
weave-agent-adapter uninstall --harness codex         # Codex
weave-agent-adapter uninstall --harness gemini-cli    # Gemini CLI
```

Use `--local` if the hooks were installed with repository scope. After
uninstalling every harness, you can remove the virtual environment and
`~/.weave-agent-adapter/` if you no longer need its configuration or
diagnostics.

## Harness support

Claude Code, Codex, and Gemini CLI expose different hook data. Missing optional
lifecycle events and metadata degrade by omission rather than breaking the
trace.

Gemini CLI captures sessions, turns, model selection, tools, final responses,
and compaction through its stable non-streaming hooks. Permissions, subagents,
transcript enrichment, and configuration fingerprinting are omitted because
that profile has no reliable source for them.

Claude Code's `PermissionDenied` hook covers auto-mode classifier denials, not
denials made in the manual permission dialog.

Codex adds best-effort transcript enrichment for intermediate assistant
messages, public reasoning summaries, model-call usage, and model names. If
those native details are unavailable, the final turn-end reply is still
emitted as a fallback LLM child.

## Runtime and privacy

Hooks perform a size- and time-bounded send to a user-only Unix socket and
always exit zero without making permission decisions. They use empty stdout by
default and may return an empty JSON object when the harness requires a JSON
acknowledgment.

The first event starts a singleton sidecar, which normalizes events, redacts
values, builds the turn, and maps it to public Weave Conversation SDK objects.
It exits when idle and restarts on demand. A turn-end hook hands the completed
turn to the emitter immediately and only once.

The Weave SDK owns agent-span routing, asynchronous export, batching, and
network retry. Delivery is deliberately best-effort: there is no raw capture,
spool, outbox, replay, or second tracing plane. Local diagnostics contain
metadata only, never payload values or exception messages.

## Configuration

Environment variables override `~/.weave-agent-adapter/config.toml`.

```toml
[weave]
project = "my-entity/my-project"  # or a bare project using the default entity
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

For advanced setups, `WANDB_ENTITY` or `[weave].entity` supplies the entity
when `project` is bare. `WEAVE_PROJECT` overrides the configured project.
`WEAVE_AGENT_ADAPTER_DISABLE=1` disables hook forwarding.

## Add a harness

Another JSON-on-stdin command-hook harness can be added with one TOML profile.
Copy a profile from
[`weave_agent_adapter/profiles/`](weave_agent_adapter/profiles/), then declare:

- native event to canonical action mappings;
- dotted JSON payload fields;
- optional thread, transcript-enrichment, and configuration-surface behavior;
- user and local settings paths plus the events to register.

Common fields may be supplemented by event-specific field paths. See the
[harness profile contract](specs/HARNESS_PROFILES.md) for the supported shape.

## Design contracts

- [Product architecture and invariants](specs/DESIGN.md)
- [Harness profile contract](specs/HARNESS_PROFILES.md)
- [Weave agent span contract](specs/WEAVE_SPAN_CONTRACT.md)

## Development

From a source checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[sidecar,dev]"
python -m pytest -q
```

The suite covers reducer correlation and lifecycle, every registered hook,
installer safety, privacy, project routing, typed span mapping, and SDK setup.

## License

See [LICENSE](LICENSE).
