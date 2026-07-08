# weave-agent-adapter specs

Detailed specs behind [`../DESIGN.md`](../DESIGN.md). Each is self-contained; `DESIGN.md` stays the high-level map.

| # | Spec | Status |
|---|---|---|
| 01 | [Data model](01-data-model.md): wire event, sidecar state, Weave-call mapping | draft |
| 02 | [Harness profiles & adapters](02-harness-profiles.md): canonical actions, adapters, event/field mapping | draft |
| 03 | [Hook & wire protocol](03-wire-protocol.md): dispatcher, socket, framing, best-effort delivery | draft |
| 04 | [Sidecar lifecycle](04-sidecar-lifecycle.md): spawn, singleton, idle shutdown, crash recovery | draft |
| 05 | [Correlation](05-correlation.md): `tool_use_id` resolution + fallbacks | draft |
| 06 | [Weave mapping](06-weave-mapping.md): what we send: call_start/end per span | draft |
| 07 | [Config](07-config.md): `config.toml`/env, active harness, redaction, sampling, delivery | draft |
| 08 | [Integration & packaging](08-integration.md): plugin, installer, CLI | draft |

Convention: specs describe *intended* behavior. Anything unverified against a harness / Weave runtime is marked **OPEN** and resolved by M0 capture or a spike.
