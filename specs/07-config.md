# Spec 07 — Config

Consumed by the sidecar (M1+); the hook needs almost none of it. Example: [`../examples/config.toml`](../examples/config.toml).

## Sources & precedence

`env var` > `config.toml` > built-in default. File location: `~/.claude/claude-weave/config.toml` (path via `CLAUDE_WEAVE_CONFIG`).

## Keys

| Key | Env override | Default | Meaning |
|---|---|---|---|
| `active_harness` | `CLAUDE_WEAVE_HARNESS` | `claude-code` | profile in `profiles/<name>.toml` (spec 02) |
| `weave.entity` | `WANDB_ENTITY` | — | Weave entity |
| `weave.project` | `WEAVE_PROJECT` | `claude-code` | Weave project |
| `weave.enable_wal` | `WEAVE_ENABLE_WAL` | `true` | crash-safe queued sends |
| `redaction.redact_pii` | `WEAVE_REDACT_PII` | `false` | Presidio PII scrubbing |
| `redaction.redact_keys` | — | `[api_key, authorization, token, password]` | key denylist |
| `sampling.session_rate` | — | `1.0` | fraction of sessions traced (root-only) |
| `trace.granularity` | — | `session` | `session` (one trace/session) or `turn` |
| `sidecar.idle_shutdown_s` | — | `120` | idle exit timeout |
| `paths.socket` / `paths.state` | — | under `~/.claude/claude-weave/` | runtime dirs |

## Secrets

`WANDB_API_KEY` is read from the **environment only** — never from `config.toml` (which may be committed). The example file and `.gitignore` reflect this.

## Redaction

Two layers, both in the sidecar (spec 06 sends already-redacted data):

1. **Weave built-ins:** `add_redact_key()` for `redaction.redact_keys`; `redact_pii`/`redact_pii_fields` via `weave.init(settings=…)`.
2. **Our postprocess:** scrub `tool_input`/`tool_output`/`prompt` (secret-shaped regex, per-tool off-switch) before the value reaches a call.

## Sampling

`session_rate` decides at the **root** `session` span; children inherit (Weave sampling is root-only). Unsampled sessions do no Weave I/O.

## OPEN

- Per-project config overrides (like `.claude/settings.json` layering) — defer unless requested.
- Exact env var names for our own keys (align with Weave's where they overlap).
