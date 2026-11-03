# weave-agent-adapter design

## Product contract

The adapter observes coding-agent command hooks and records each completed turn
in a CoreWeave Evaluations (CES) Dataset. A session is a Trajectory rooted at
its session Case. Each turn adds a Case per user message, assistant message,
tool call, and compaction, carrying permission decisions, steering, and the
available model and token details. A subagent gets its own Trajectory, rooted
at the tool call that launched it. Monopole reads these sessions to score and
judge them.

Recording is passive. It never approves, rejects, rewrites, or delays an agent
action beyond a short bounded local handoff. Hook, sidecar, and send failures
never fail the harness.

Claude Code, Codex, and Gemini CLI ship as declarative profiles. Claude Code
and Codex sessions are read from their transcripts. Gemini CLI's turns are
built from hook events by the reducer, and they are not sent to CES until its
transcript format is verified. Another harness can use the same hook path when
it runs a command for lifecycle events and passes one JSON object on standard
input.

## Architecture

```text
harness command hook
  -> bounded Unix-socket handoff
  -> singleton sidecar
  -> session sender: hook facts + transcript reader -> one job per finished turn
  -> CesWriter: session Case, session Trajectory, then the turn's Cases, in order
  -> CES Dataset
```

The hook path is standard-library-only. It stamps the entry time, reads one
size- and time-bounded JSON object, sends a versioned envelope, and exits zero.
Stdout is empty by default; a generic command flag emits an empty JSON object
for harnesses that require a JSON acknowledgment. A missing sidecar is started
once and retried within the same short deadline. No hook performs network I/O
or writes raw payloads.

The sidecar owns profile loading, field extraction, redaction, transcript
reading, and CES writes. One sidecar multiplexes harnesses and sessions. The
session sender keeps what the transcripts lack, such as permission prompts, the
permission mode, and subagent transcript paths. When a turn ends, it hands a
job to its worker thread. A harness without a transcript reader gets a reducer
(`tracer.py`) that builds its turns from hook events instead.

Profiles map native events and common or event-specific payload fields to a
fixed canonical vocabulary. The reducer contains no harness names or native
event names. Harnesses that omit an event or field lose only that detail rather
than requiring alternate code paths.

## State and lifecycle

A Codex `Stop` names the turn it ends. A Claude Code `Stop` ends the
transcript's last turn, unless background tasks are still running. When a
session goes idle past its TTL, every turn not yet written is sent as final,
which covers interrupted turns and turns whose `Stop` was skipped. A parent
turn's job carries the finished turns of its subagents.

The writer keeps each session's CES IDs and written turns in a per-session
file, so a restarted sidecar continues the same Trajectory and never resends a
written turn. The sidecar exits after its idle deadline only when no job is
pending. Shutdown gives queued jobs a bounded time, then moves what is left to
the dead-letter file.

## Reliability and privacy

Every write carries an idempotency key built from the turn, so a retry or a
replay never duplicates a Case. Rate limits and unavailability are retried. A
turn that still fails is kept whole in the dead-letter file, and
`weave-agent-adapter evaluations replay` reruns it from the top. A hook-built
turn that is still open when the sidecar dies is lost.

Redaction occurs before any network output. Sensitive keys, common token
shapes, JWTs, AWS access-key IDs, and complete PEM private-key blocks are
replaced. Rotating diagnostics contain phase and type metadata, never payload
values or exception messages. Runtime files are user-only.

## Deliberate boundaries

- Hook and sidecar remain separate for latency and dependency isolation.
- Profile normalization and turn building remain separate, so a new
  command-hook harness adds a profile and, to reach CES, a transcript reader,
  not branches in shared code.
- Transcript formats are not part of this adapter's contract. Each reader is
  the only code that knows its format.
- Capabilities are additive: session end, explicit failures, permissions,
  subagents, compaction, model metadata, configuration surfaces, and
  pending-work signals are all optional.
- `records.py` and [EVALUATIONS_CASE_CONTRACT.md](EVALUATIONS_CASE_CONTRACT.md)
  own the CES field contract.

## Non-goals

- In-process harness instrumentation.
- Hook mechanisms other than JSON on standard input.
- A plugin packaging or marketplace workflow.
- Splitting a turn that exceeds one CES request.
