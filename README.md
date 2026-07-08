# weave-agent-adapter

Trace agent-harness sessions to [Weights & Biases Weave](https://wandb.ai/site/weave) — as a nested trace you can inspect, filter, and analyze. Two harnesses ship as profiles: [Claude Code](https://docs.claude.com/en/docs/claude-code) and [Codex](https://developers.openai.com/codex).

> **Status:** M0–M5 implemented and tested (28-test suite). Claude Code payloads verified against a real capture. Installable from source (`pip install -e .` + `weave-agent-adapter install`); not yet published to PyPI.

## What it captures

A session becomes one Weave trace, nested to match what actually happened:

```
session
└── turn                     "add logging to auth.py"
    ├── input                the prompt
    ├── tool:Bash            approved (auto)
    ├── tool:Edit            rejected · deny "use the logger, not print"
    ├── steering             user redirect mid-turn
    └── tool:Edit            approved (user)
```

Beyond the call tree, it records the human-in-the-loop signals: **approval**, **rejection**, and **steering**.

## Quickstart

Install (the `sidecar` extra pulls in Weave; the hook itself is stdlib-only):

```bash
pip install "weave-agent-adapter[sidecar]"      # from source: pip install -e ".[sidecar]"
```

Point it at your Weave project. The API key is read from the environment only — never written to disk:

```bash
export WANDB_API_KEY=...                          # your W&B API key
export WEAVE_PROJECT=my-entity/my-project
```

Register the hooks for your harness (edits that harness's own settings file; idempotent and removable):

```bash
weave-agent-adapter install                       # Claude Code (default)
weave-agent-adapter install --harness codex       # Codex
```

Now use your agent normally — each session appears in Weave as a nested trace. The sidecar starts on the first event and scales to zero when idle. To remove the hooks:

```bash
weave-agent-adapter uninstall [--harness codex]
```

## How it works

Non-intrusive by design — the harness itself is never modified.

- **Hooks** (external one-line commands, auto-registered by a plugin) emit each event to a local socket and exit immediately.
- A **sidecar** — spawned at session start, one warm process per machine — hosts a single `weave.init()` client and turns events into nested Weave calls. A long-lived process is what lets the SDK's async batching and retry apply, and keeps spans in Weave's native call model (rather than raw OTel JSON).

Adopters write **zero** lines of code: installing the plugin registers everything.

See [DESIGN.md](DESIGN.md) for the full design, [specs/](specs/) for detailed specs, and [examples/](examples/) for copy-pasteable config.

## Roadmap

- [x] **M0** — capture mode; verified against a real Claude Code session (`tool_use_id` confirmed as the per-call correlation id). Permission-event payloads still to capture interactively.
- [x] **M1** — sidecar + core trace tree (session / turn / tool)
- [x] **M2** — permission / approval / rejection / steering
- [x] **M3** — redaction, sampling, config
- [x] **M4** — subagents, compaction, hardening
- [x] **M5** — plugin + pip packaging

## License

See [LICENSE](LICENSE).
