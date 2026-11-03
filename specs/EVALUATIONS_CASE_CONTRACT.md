# CES session Cases

With `[evaluations]` enabled, the sidecar writes each finished agent turn to a CoreWeave Evaluations (CES) Dataset. Hooks stay fast: a worker thread in the sidecar reads the harness transcript and sends the writes.

## Layout

```text
session Case ── root of ──> session Trajectory S
                               ├── member Cases, one per user message, assistant message, tool call, and compaction
                               └── subagent Trajectory (root: the tool_call Case that launched it, parent: S)
                                     └── the subagent's member Cases, and its own subagents the same way
```

- **The session Case** holds the session's first-turn facts. It is written once and never changes.
- **Member Cases** carry `trajectory_id` (S, or their subagent's Trajectory) and `session_trajectory_id` (always S), so a reader can find the session from any Case.
- **A turn's Cases** share `turn_id`. The turn's first Case, its user message, has `step_index` 0 and carries the turn totals, and it is the Case that takes the turn's Scores.
- **Codex operations**, the commands an exec tool runs, fold into their `tool_call` Case as `exit_code`, `error`, and `duration_ms`.

## Field contract

CES types a Dataset field from its first value and never retypes it, so each field name has one type across every kind. Every Case of a kind carries the same keys, and an unavailable value is null. `weave_agent_adapter/records.py` is the source of truth, and `evaluations setup` declares these fields.

| Group | Input fields |
| --- | --- |
| Every Case | `kind` (`session`, `user_message`, `assistant_message`, `tool_call`, `compaction`), `harness`, `session_id`, `trace_role`, `adapter_version` |
| Session Case | `cwd`, `model`, `config_version`, `started_at` |
| Member Cases | `session_trajectory_id`, `turn_id`, `turn_index`, `step_index`, `agent_id` (null on the main thread), `started_at`, `ended_at`, `model`, `effort`, `cwd`, `git_branch`, `config_version`, `permission_mode` |
| `user_message` | `text`, `steering` (true for a message typed mid-turn) |
| `tool_call` | `call_id`, `tool_name`, `arguments` (JSON) |
| `compaction` | `trigger` |

| Group | Output fields |
| --- | --- |
| `assistant_message` | `text`, `reasoning`, `finish_reason` (`tool_calls`, `stop`, or null for a call that never got a response), `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens` |
| `tool_call` | `status`, `output`, `output_truncated`, `error`, `exit_code`, `duration_ms`, `permission_decision` (`approved`, `not_approved`, or null without a prompt) |
| Turn totals, on `step_index` 0 | `turn_status` (`completed`, `aborted`, `incomplete`), `turn_duration_ms`, `turn_{input,output,cache_read,cache_write,reasoning}_tokens`, `turn_{steering,denial,tool_error,compaction}_count` |

- Timestamps are RFC 3339 UTC with milliseconds, so string order is time order.
- Text values are capped at 32,000 characters, ending in `…[truncated]`.
- The redactor scrubs values, never field names.

## Writes for one turn

Each request's Idempotency-Key comes from what it writes, and a replay sends a byte-identical body, so CES answers a repeat with the original IDs.

| Request | Body | Key |
| --- | --- | --- |
| `PATCH .../datasets/{d}/cases` | the session Case | `session-v1:{harness}:{session_id}` |
| `POST .../datasets/{d}/trajectories` | `{name, root_case_id}` | `session-trajectory-v1:{harness}:{session_id}` |
| `PATCH .../datasets/{d}/cases` | the turn's main-thread Cases | `turn-v1:{harness}:{session_id}:{turn_id}` |
| `POST .../datasets/{d}/trajectories` | `{name, root_case_id, parent_trajectory_id}` for a new subagent | `subagent-v1:{harness}:{session_id}:{agent_id}` |
| `PATCH .../datasets/{d}/cases` | that subagent's Cases for the turn | `turn-v1:{harness}:{session_id}:{agent_turn_id}` |

- **The session file**, `~/.weave-agent-adapter/evaluations-sessions/{harness}-{session_id}.json`, keeps the session Case, S, and subagent IDs. Each body is saved before it is sent, so after a crash between sending and saving, the stored body is resent and CES returns the original ID.
- **Subagent turns** are written with the parent turn that ran them, parents first. Codex's `SubagentStop` names the subagent's transcript, Claude Code keeps them in the session's `subagents` directory, and the parent's `Stop` sends their finished turns. A Claude Code `Stop` with background tasks still running sends nothing.
- **Retries:** the client retries network errors, 429, and 5xx three times, honoring `Retry-After`.
- **Dead letters:** a turn that still fails is one line in `~/.weave-agent-adapter/evaluations-dlq.jsonl` holding the whole turn job (format v2). `weave-agent-adapter evaluations replay` reruns each job from the top.
- **409 `idempotency_key_reuse`** on a turn counts as already stored.
- **Size:** one turn is one request. CES takes at most 10,000 Cases and a 16 MiB body per request. A 413 is logged as `ces_turn_too_large`, and the turn stays in the dead-letter file until turns are split.

## Setup

```toml
[evaluations]
enabled = true
project = "entity/project"      # required
# name = "agent-sessions"       # the Dataset
# base_url = "https://evaluations.wandb.ai"
# wandb_base_url = "https://api.wandb.ai"   # its ~/.netrc entry holds the API key
# rollout_context = false       # turn on once CES serves wandb/core#60301
```

**`rollout_context` (temporary switch).** With it on, each new session Trajectory carries CES's rollout context v1 (role `evaluated_rollout`), and each subagent Trajectory carries role `child_agent`. Both use the experiment Dataset ID and the request's Idempotency-Key as `client_record_key`, with null run and trace references. Frozen Trajectory reads include only Trajectories that have a context, and a context can't be added after creation. So turn the switch on as soon as the CES being written to serves wandb/core#60301. Once every CES does, make it the default and delete the switch. A session keeps the Trajectory body it started with, so turning it on mid-session leaves that session's earlier Trajectories without one.

`weave-agent-adapter evaluations setup` resolves the project's `scope_ref` through GraphQL, creates the Dataset (key `dataset-v1:{project}:{name}`), declares the fields, and writes `{project, name, scope_ref, dataset_id}` to `~/.weave-agent-adapter/evaluations.json`. The sidecar sends nothing until that file matches the config.

Codex and Claude Code have transcript readers. Claude Code turns run from one user prompt to the next, its assistant rows merge by `message.id`, and a subagent file's `.meta.json` `toolUseId` names the Task call its Trajectory starts from. Gemini CLI events are ignored until its reader lands.
