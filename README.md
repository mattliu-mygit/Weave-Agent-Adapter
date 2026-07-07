# claude-weave

Trace agent-harness sessions to [Weights & Biases Weave](https://wandb.ai/site/weave) — as a nested trace you can inspect, filter, and analyze. [Claude Code](https://docs.claude.com/en/docs/claude-code) is the first supported harness.

> **Status:** early development. Design is settled; the `M0` capture hook is written (inspector + real-session confirmation still pending). Not yet installable.

## What it captures

A session becomes one Weave trace, nested to match what actually happened:

```
session
└── turn                     "add logging to auth.py"
    ├── input                the prompt
    ├── tool:Bash            approved (auto)
    ├── tool:Edit            rejected
    │   └── permission       deny · "use the logger, not print"
    ├── steering             user redirect mid-turn
    └── tool:Edit            approved (user)
```

Beyond the call tree, it records the human-in-the-loop signals: **approval**, **rejection**, and **steering**.

## How it works

Non-intrusive by design — the harness itself is never modified.

- **Hooks** (external one-line commands, auto-registered by a plugin) emit each event to a local socket and exit immediately.
- A **sidecar** — spawned at session start, one warm process per machine — hosts a single `weave.init()` client and turns events into nested Weave calls. This is what makes Weave's async batching, retry, WAL, and redaction actually apply (they need a long-lived process).

Adopters write **zero** lines of code: installing the plugin registers everything. A daemonless OTLP-direct mode is kept as a fallback.

See [DESIGN.md](DESIGN.md) for the full design, [specs/](specs/) for detailed specs, and [examples/](examples/) for copy-pasteable config.

## Roadmap

- [ ] **M0** — capture mode (hook written; inspector + real-session schema/correlation confirmation pending)
- [ ] **M1** — sidecar + core trace tree (session / turn / tool)
- [ ] **M2** — permission / approval / rejection / steering
- [ ] **M3** — redaction, sampling, WAL, config
- [ ] **M4** — crash reconciliation, subagents, OTLP fallback
- [ ] **M5** — plugin + pip packaging

## License

See [LICENSE](LICENSE).
