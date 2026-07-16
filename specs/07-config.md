# Spec 07: Configuration and privacy

The sidecar reads `~/.weave-agent-adapter/config.toml` unless
`WEAVE_AGENT_ADAPTER_CONFIG` selects another path. Environment overrides take
precedence. The hook reads only its socket, disable, and diagnostic environment
variables so it remains fast and standard-library-only.

Key settings are the Weave project, optional per-repository routing, redaction
switch/extra keys, session sampling rate, idle shutdown, session TTL, and turn
linger. A bare project is combined with the authenticated W&B default entity;
an explicit `entity/project` is used verbatim. `WANDB_API_KEY` comes from the
environment or the W&B netrc login, never from adapter config.

`WEAVE_AGENT_ADAPTER_DISABLE` accepts explicit truthy values (`1`, `true`,
`yes`, `on`); `0`, `false`, `no`, and an empty value leave tracing enabled.
`WEAVE_AGENT_ADAPTER_OTLP_ENDPOINT` overrides the documented Weave endpoint.
`WEAVE_AGENT_ADAPTER_LOG` overrides the rotating diagnostic log path.

Redaction is default-on before every debug/network sink. Dictionary keys
containing configured sensitive terms lose their entire value. String patterns
remove common API tokens, JWTs, AWS access-key IDs, and complete multiline PEM
private-key blocks. Diagnostics never contain payload values or exception
messages and are written with user-only permissions.
