# Spec 03: Hook and wire protocol

The command-hook entrypoint is:

```text
weave-agent-adapter hook --harness NAME --event NAME
```

Both arguments come from the harness profile. The hook stamps `captured_at` on
entry, reads one JSON object from stdin within 0.5 seconds and 1 MiB, wraps it as
`{v, harness, event, captured_at, pid, payload}`, and attempts a bounded send to
the local Unix stream socket. Partial input, oversized input, malformed JSON,
or a non-object payload is diagnosed without forwarding a fabricated event.

If the socket is absent, any hook may detach the sidecar and retry only within a
short total deadline. Failure is best-effort: diagnose metadata only, exit zero,
and never persist the raw payload. Stdout stays empty and the hook never makes a
permission decision.

Messages are newline-delimited JSON. Wire version 1 is the only accepted
version; unknown versions and invalid envelope fields are logged and dropped.
The socket defaults to `~/.weave-agent-adapter/sidecar.sock`, is mode `0600`, and
can be overridden with `WEAVE_AGENT_ADAPTER_SOCKET`.
