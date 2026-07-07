# Configuration examples

Copy-pasteable configs. Replace `/path/to/claude-weave` with this repo's absolute path.

## Files

| File | Status | Use |
|---|---|---|
| [`claude-code.settings.capture.json`](claude-code.settings.capture.json) | **works now (M0)** | Capture-mode hooks — dumps raw payloads to `~/.claude/claude-weave/capture/` |
| [`config.toml`](config.toml) | intended (M1+) | Sidecar config — Weave project, redaction, sampling, WAL |

The harness profile itself lives at [`../profiles/claude-code.toml`](../profiles/claude-code.toml).

## Enable M0 capture now

1. Merge `claude-code.settings.capture.json` into your Claude Code settings (`~/.claude/settings.json` for all projects, or `.claude/settings.json` in a project), replacing the path.
2. Use Claude Code normally.
3. Inspect the dumped payloads under `~/.claude/claude-weave/capture/<session_id>/`.

## Target production wiring (M5, once packaged — not yet available)

After `pip install` + the plugin, no hand-editing is needed. The equivalent manual block is a single static command per event, with `--event` set per entry:

```json
{
  "hooks": {
    "PreToolUse": [{ "matcher": "*", "hooks": [{ "type": "command", "command": "claude-weave hook --harness claude-code --event PreToolUse" }] }]
  }
}
```

...repeated for each event in the profile's `[registration].events`.
