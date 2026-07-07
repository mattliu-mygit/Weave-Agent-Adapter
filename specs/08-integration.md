# Spec 08 — Integration & packaging

Goal: adopters write **zero** lines. Everything derives from the active profile's `[registration]` (spec 02). Kept light on purpose — detail lands at M5.

## Delivery ladder (least → most effort)

1. **Plugin** — ships a generated `hooks/hooks.json`; install → done. 0 authored lines. The floor.
2. **`claude-weave install [--user|--project]`** — patches the harness config from the profile's `[registration]`; idempotent, `uninstall` removes only our entries.
3. **Manual** — paste the generated block.

## CLI

`claude-weave hook` (dispatcher, spec 03) · `install` / `uninstall` · `sidecar` (foreground, for debug) · `doctor` (diagnose setup).

## Packaging

- pip package `claude-weave`; `console_scripts` entry point `claude-weave`.
- `weave` is a sidecar-only dependency; the hook path stays stdlib-only (fast, and works even if `weave` isn't importable).

## OPEN

- Exact plugin manifest format per harness (confirm at build time).
- One package with a `weave` extra, or split hook/sidecar.
