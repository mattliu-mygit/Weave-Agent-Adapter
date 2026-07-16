# weave-agent-adapter specs

Detailed specs behind [`DESIGN.md`](DESIGN.md). Each is self-contained; `DESIGN.md` stays the high-level map.

| # | Spec | Status |
|---|---|---|
| 01 | [Data model](01-data-model.md): wire event and sidecar state | current |
| 02 | [Harness profiles & adapters](02-harness-profiles.md): canonical actions, adapters, event/field mapping | draft |
| 03 | [Hook & wire protocol](03-wire-protocol.md): dispatcher, socket, framing, best-effort delivery | current |
| 04 | [Sidecar lifecycle](04-sidecar-lifecycle.md): singleton, idle shutdown, crash recovery | current |
| 05 | [Correlation](05-correlation.md): `tool_use_id` resolution + fallbacks | draft |
| 06 | [Weave mapping](06-weave-mapping.md): OTel spans sent to Weave | current |
| 07 | [Config](07-config.md): config/env, redaction, sampling, delivery | current |
| 08 | [Integration & packaging](08-integration.md): plugin, installer, CLI | draft |

Convention: specs describe *intended* behavior. Anything unverified against a harness / Weave runtime is marked **OPEN** and resolved by M0 capture or a spike.
