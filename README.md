# weave-agent-adapter

See what your coding agents did and what you allowed them to do.
`weave-agent-adapter` records each finished agent turn in the CoreWeave
Evaluations service (CES): the user's messages, the assistant's replies, every
tool call with its approval or rejection, compactions, and subagent runs.
[Monopole](https://github.com/mattliu-mygit/Monopole) scores and judges the
sessions it records.

It attaches through external command hooks: no harness source changes, no
in-process SDK, and no synchronous network work on the hook path. Claude Code,
Codex, and Gemini CLI profiles ship with the package. Claude Code and Codex
sessions reach CES; Gemini CLI sessions don't yet.

## What gets recorded

```text
claude-code session 7f3c…            a session Trajectory
├── session                          its session Case
├── user_message       "add logging"
├── assistant_message  1.2k input / 80 output tokens
├── tool_call Bash     approved
├── tool_call Edit     rejected: "use the logger"
├── tool_call Task     roots a subagent's own Trajectory
└── assistant_message  "done"
```

Each finished turn is written once, in order, under keys built from its
content, so a sidecar restart or a replay never duplicates it.
[specs/EVALUATIONS_CASE_CONTRACT.md](specs/EVALUATIONS_CASE_CONTRACT.md) has the
layout, the field contract, and the write order.

## Quickstart

### Prerequisites

You need:

- Python 3.10 or newer;
- a [Weights & Biases account](https://wandb.ai/) with a project to hold the
  sessions;
- Claude Code or Codex.

The examples below use a POSIX shell on macOS or Linux.

### 1. Install

Choose either the published package or a source checkout. Both use a virtual
environment so the adapter stays isolated.

#### From PyPI

```bash
python3 -m venv ~/.venvs/weave-agent-adapter
source ~/.venvs/weave-agent-adapter/bin/activate
python -m pip install --upgrade pip
python -m pip install weave-agent-adapter
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
python -m pip install -e .
```

Keep the environment active through hook registration. The installer records
the adapter executable's absolute path, so the hooks keep working after you
deactivate the environment. Reinstall the hooks if you later move or recreate
that environment.

### 2. Authenticate with W&B

CES authorizes the adapter with the W&B API key that `~/.netrc` holds for the
W&B host, which `wandb login` writes. The adapter never reads `WANDB_API_KEY`.
Never put the API key in the adapter configuration file.

### 3. Choose a project and set up its Dataset

Create `~/.weave-agent-adapter/config.toml`:

```toml
[evaluations]
enabled = true
project = "my-entity/my-project"
```

Replace both names with your W&B entity and project, then create the CES
Dataset that holds the sessions:

```bash
weave-agent-adapter evaluations setup
```

Setup declares the Dataset's fields and saves its IDs to
`~/.weave-agent-adapter/evaluations.json`. The sidecar sends nothing until that
file matches the configuration.

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
3. Check that the turn reached CES, for example with Monopole's
   `weave-agent-signals inspect --recent 1`.

A turn is sent in the background once it ends. On a healthy run the diagnostic
log may be empty. If the turn doesn't arrive, inspect the metadata-only log and
the dead-letter file:

```bash
tail -n 50 ~/.weave-agent-adapter/adapter.log
cat ~/.weave-agent-adapter/evaluations-dlq.jsonl
```

While the sidecar is active, its user-only socket and singleton lock live at:

```text
~/.weave-agent-adapter/sidecar.sock
~/.weave-agent-adapter/sidecar.sock.lock
```

The socket disappears when the sidecar shuts down after its idle timeout. An
event the sidecar can't take, for example while it is still starting, waits in
`~/.weave-agent-adapter/spool.jsonl` (at most 4 MiB) until the sidecar reads it.

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

Claude Code and Codex sessions are read from their transcripts, and the hooks
add what the transcripts lack: permission prompts, the permission mode, and
subagent transcript paths. Missing optional hook data degrades by omission.

Gemini CLI's hooks register, and the sidecar builds its turns from hook events,
but those turns aren't sent to CES yet, because Gemini's transcript format is
unverified.

Claude Code's `PermissionDenied` hook covers auto-mode classifier denials, not
denials made in the manual permission dialog.

## Runtime and privacy

Hooks perform a size- and time-bounded send to a user-only Unix socket and
always exit zero without making permission decisions. They use empty stdout by
default and may return an empty JSON object when the harness requires a JSON
acknowledgment.

The first event starts a singleton sidecar. When a turn ends, a background
thread reads the transcript, redacts values, builds the turn's Cases, and writes
them to CES. A turn that still fails after retries is kept whole in the
dead-letter file; `weave-agent-adapter evaluations replay` resends it. If a
session's turns fail to build from its transcript, the file records the
transcript instead, and replay builds the turns again from it. The
sidecar exits when idle and restarts on demand. Local diagnostics contain
metadata only, never payload values or exception messages.

## Configuration

Environment variables override `~/.weave-agent-adapter/config.toml`.

```toml
[evaluations]
enabled = true
project = "my-entity/my-project"
name = "agent-sessions"              # the CES Dataset that holds the sessions
harnesses = ["claude-code", "codex", "gemini-cli"]

[redaction]
enabled = true
redact_keys = ["api_key", "authorization", "token", "password"]

[sampling]
session_rate = 1.0                   # fraction of sessions sent to CES

[sidecar]
idle_shutdown_s = 120
session_ttl_s = 3600
```

`WEAVE_AGENT_ADAPTER_DISABLE=1` disables hook forwarding.

### Workspace trace role

Every Case carries a `trace_role`. Normal coding-agent work defaults to
`agent_session`. A workspace can persist another role in the ignored file
`.weave-agent-adapter/trace-role`, containing one of:

- `agent_session`
- `signal_evaluation`
- `judge_evaluation`
- `reflection_evaluation`
- `other_system`

For example:

```bash
mkdir -p .weave-agent-adapter
printf '%s\n' 'judge_evaluation' > .weave-agent-adapter/trace-role
```

The hook uses `WEAVE_AGENT_TRACE_ROLE` when it is non-empty; otherwise it looks
for the nearest workspace selector while walking from the event's working
directory through the nearest Git repository root. Outside a Git repository it
checks only the event's working directory. With neither source it uses
`agent_session`. Unknown explicit values fail safe to `other_system`.

The repository-local `.weave-agent-adapter/` directory is ignored by Git. It
is separate from the user-level `~/.weave-agent-adapter/` directory that holds
project configuration and diagnostics.

## Evaluations commands

```bash
weave-agent-adapter evaluations setup    # create the Dataset, declare its fields, save its IDs
weave-agent-adapter evaluations replay   # resend turns that failed, from the dead-letter file
weave-agent-adapter evaluations status   # how many turns wait in the dead-letter file, and why
weave-agent-adapter evaluations log-transcript --harness codex --file run.jsonl [--dataset ab-arms]
                                         # log one finished session file; prints its session Case and Trajectory IDs
```

## Add a harness

Another JSON-on-stdin command-hook harness can be added with one TOML profile.
Copy a profile from
[`weave_agent_adapter/profiles/`](weave_agent_adapter/profiles/), then declare:

- native event to canonical action mappings;
- dotted JSON payload fields;
- optional thread and configuration-surface behavior;
- user and local settings paths plus the events to register.

Common fields may be supplemented by event-specific field paths. See the
[harness profile contract](specs/HARNESS_PROFILES.md) for the supported shape.
A harness reaches CES once it also has a transcript reader, like
`codex_transcript.py` and `claude_transcript.py`.

## Design contracts

- [Product architecture and invariants](specs/DESIGN.md)
- [Harness profile contract](specs/HARNESS_PROFILES.md)
- [CES session Case contract](specs/EVALUATIONS_CASE_CONTRACT.md)

## Development

From a source checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest -q
```

The suite covers reducer correlation and lifecycle, every registered hook,
installer safety, privacy, the transcript readers, CES writes and retries, and
the dead-letter file.
