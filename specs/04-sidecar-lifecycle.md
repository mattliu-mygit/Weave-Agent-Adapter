# Spec 04: Sidecar lifecycle

The sidecar is a detached singleton guarded by an advisory lock beside its Unix
socket. It multiplexes sessions from all configured harnesses and keeps one
profile-driven tracer per harness.

Socket inactivity alone cannot terminate active work. A tracer is active while
it has an open turn, running tool or subagent, or a finalized turn not yet
accepted by its emitter. Once no work is active and the idle deadline passes,
the sidecar closes the socket and exits. A later hook starts it again.

A periodic sweep closes sessions inactive beyond `session_ttl_s`; genuinely
open work is tagged incomplete. `turn_linger_s` emits the final closed turn of a
session that has no session-end hook, but never while a child is still open.

SIGTERM and SIGINT request shutdown. The sidecar finalizes remaining state,
requests a bounded flush from each OTel provider, and diagnoses failure. State
and delivery are best-effort and in memory: crashes can lose in-flight work.
There is no spool, outbox, replay, or dead-letter subsystem.
