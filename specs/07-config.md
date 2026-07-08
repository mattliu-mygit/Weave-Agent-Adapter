# Spec 07: Config

Consumed by the sidecar (M1+); the hook needs almost none of it. Example: [`../examples/config.toml`](../examples/config.toml).

## Sources & precedence

`env var` > `config.toml` > built-in default. File location: `~/.weave-agent-adapter/config.toml` (path via `WEAVE_AGENT_ADAPTER_CONFIG`).

Only the **sidecar** reads `config.toml`. The **hook stays parse-free** (spec 03), so its paths (capture dir, socket) arrive via env vars, set by the installer from config, defaulting to a harness-neutral `~/.weave-agent-adapter/`. The runtime dir is deliberately *not* under a harness's own config dir (e.g. `.claude/`).

## Keys

| Key | Env override | Default | Meaning |
|---|---|---|---|
| `active_harness` | `WEAVE_AGENT_ADAPTER_HARNESS` | `claude-code` | profile in `weave_agent_adapter/profiles/<name>.toml` (spec 02) |
| `weave.entity` | `WANDB_ENTITY` |   | Weave entity |
| `weave.project` | `WEAVE_PROJECT` | `claude-code` | Weave project |
| `weave.enable_disk_fallback` | `WEAVE_ENABLE_DISK_FALLBACK` | `true` | SDK dead-letter log for sends that fail after retries (not replay-on-restart) |
| `redaction.enabled` |   | `true` | master switch for our `Redactor` |
| `redaction.redact_keys` |   | Redactor defaults | extra sensitive keys to deny |
| `sampling.session_rate` |   | `1.0` | fraction of sessions traced (root-only) |
| `trace.granularity` |   | `session` | `session` (one trace/session) or `turn` |
| `sidecar.idle_shutdown_s` |   | `120` | idle exit timeout |
| `sidecar.session_ttl_s` |   | `3600` | finalize + drop sessions idle past this (crash safety) |
| `paths.socket` / `paths.state` |   | under `~/.weave-agent-adapter/` | runtime dirs |

## Secrets

`WANDB_API_KEY` is read from the **environment only**, never from `config.toml` (which may be committed). The example file and `.gitignore` reflect this.

## Redaction

Done by our own `Redactor` (`redact.py`) in the **tracer**, before any sink, so Weave, debug, and every sink get already-redacted data. (We can't lean on Weave's `redact_keys`/`redact_pii`: those hook the `@weave.op` path, and we use the low-level `call_start`/`call_end` API.) Default-on. Two rules:

1. **Key denylist**, a dict key containing `api_key`, `authorization`, `token`, `secret`, `password`, … → the whole value becomes `[REDACTED]`.
2. **Secret-shaped patterns**, scrub substrings matching known key shapes (`sk-…`, `wandb_v1_…`, `gh*_…`, AWS `AKIA…`, JWTs, PEM blocks) anywhere in strings.

Applied to `tool_input`, `tool_output`, and the prompt. Deny keys/enabled are configurable (`redaction.*`).

## Sampling

`session_rate` decides at the **root** `session` span; children inherit (Weave sampling is root-only). Unsampled sessions do no Weave I/O.

## OPEN

- Per-project config overrides (like `.claude/settings.json` layering), defer unless requested.
- Exact env var names for our own keys (align with Weave's where they overlap).
