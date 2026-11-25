# Harness profile contract

## Purpose

A profile tells the adapter three things about one command-hook harness: how to
register its hooks, where a hook payload holds the session's working directory,
and which files make up its `config_version`. Hook events and transcripts are
read by code, the session sender and one transcript reader per harness, not
through the profile. So a harness's hooks need only a TOML profile, while
recording its sessions also needs a transcript reader.

The supported harness contract is intentionally narrow: the harness runs a
command for lifecycle events and passes one JSON object on standard input.
Other delivery mechanisms are outside the current product.

## Profile shape

```toml
[harness]
name = "my-harness"

[fields]
cwd = "cwd"

[config_surface]
paths   = ["~/.my-harness/AGENTS.md", "~/.my-harness/skills", "{cwd}/AGENTS.md"]
exclude = ["~/.my-harness/skills/.system"]

[registration]
user_path  = "~/.my-harness/hooks.json"
local_path = ".my-harness/hooks.json"
command    = "weave-agent-adapter hook --harness my-harness"
events     = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
```

`[fields]` holds `cwd`, a dotted path to the session's working directory in a
hook payload. The hook uses it to find the workspace's `trace_role` file; without
it, `trace_role` comes from the environment or the default.

`[config_surface]` is optional; without it `config_version` is null.

- `paths` lists the files and folders whose content is hashed into
  `config_version`, so Scores can be compared before and after an instruction
  change. `~` is the home folder, `{cwd}` the session's working directory from
  its hooks, and `{cwd_slug}` that directory with `/` replaced by `-`, as in
  `~/.claude/projects`. An entry that needs `{cwd}` is skipped when no hook sent
  one, and a missing path is skipped. Folders are walked with symlinks followed,
  and a symlink's target counts under the path the profile shows, so moving the
  target keeps the version. `.git`, `__pycache__`, `.DS_Store`, and `.pyc` files
  are skipped, and at most 1,000 files of up to 1 MiB each are read.
- `exclude` lists paths under `paths` to leave out, with the same placeholders.
  A path matches as seen through the profile entry, so it works whether a folder
  on the way is a symlink or not. Use it for files a harness or tool rewrites by
  itself, such as Codex's bundled skills in `~/.codex/skills/.system`.

Leave out anything that changes without an edit by the user, such as
auto-memory or a settings file that records trust and auth, or every session
gets a new version.

## Hook events the session sender reads

The session sender reads native event names and payload keys directly:

- every event may carry `session_id`, `cwd`, `transcript_path`, and
  `permission_mode`;
- `PermissionRequest` and the `PostToolUse` of the same `tool_use_id` give the
  call's `permission_decision`, `approved` or `not_approved`;
- `Stop` ends a turn. Codex's names it with `turn_id`. Claude Code's ends the
  transcript's last turn, unless `background_tasks` are still running;
- `SubagentStop` with `agent_transcript_path` names a subagent's transcript;
- `SessionEnd` sends an unended last turn as final.

A harness that lacks an event loses only what that event gives. Without
`PermissionRequest`, decisions are null; Claude Code then falls back to the
decision its transcript records. Without `SessionEnd`, as in Codex, a last turn
that never gets its `Stop` is never sent.

## Registration

`weave-agent-adapter install --harness NAME` reads `[registration]`, resolves
the installed console script, and atomically merges one command per event into
the harness's JSON settings. Foreign settings and hooks are preserved.
`uninstall` removes only adapter commands.

The shipped harnesses use the standard `{"hooks": {event: [...]}}` settings
shape. A future harness with a different settings format would require an
explicit product decision rather than an unused profile selector.

The registration command may include `--success-json` when a harness requires
a successful hook to print `{}`. This behavior is opt-in and does not require
the hook path to load a profile or branch on a harness name.

A profile may declare a `post_install` note for a required activation step,
such as reviewing a newly registered hook in the harness trust UI. The CLI
prints the note without adding harness-specific installer behavior.

## Adding a harness

1. Verify its event names, JSON payload fields, settings location, and stable
   tool-call identifier from authoritative documentation or a private capture.
2. Add the profile under `weave_agent_adapter/profiles/`.
3. Add installer tests covering its registration path, and hook tests covering
   the events the harness actually emits.
4. To send its sessions to CES, add a transcript reader, register it in the
   session sender, and add tests that drive a session through it. Events whose
   names or keys differ from the ones above need session-sender changes too.
5. Document genuine degradation where the harness lacks an event.

Raw payload capture is not a shipped runtime feature because it conflicts with
the adapter's privacy boundary.
