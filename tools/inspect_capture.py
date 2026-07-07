"""claude-weave capture inspector (M0).

Reads the raw hook payloads that `hook.py` dumped during a real session and
reports, per event, how often it fired and what its payload contains.

Answers the M0 open questions that gate the profile (spec 02) and correlation
(spec 05):
  1. Each event's payload schema  -> profile [fields] paths
  2. Is there a stable per-tool-call id linking PreToolUse / PostToolUse /
     permission events?           -> correlation strategy

Usage:
    python3 tools/inspect_capture.py [capture_dir]
    (default ~/.claude/claude-weave/capture, or $CLAUDE_WEAVE_CAPTURE_DIR)
"""
from __future__ import annotations

import glob
import json
import os
import sys
from collections import defaultdict

DEFAULT_DIR = os.environ.get(
    "CLAUDE_WEAVE_CAPTURE_DIR", os.path.expanduser("~/.claude/claude-weave/capture")
)

TOOL_EVENTS = {
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PermissionRequest", "PermissionDenied",
}

# Canonical fields the profile needs; we try to locate each in the payloads.
WANTED_FIELDS = [
    "session_id", "tool_name", "tool_input", "tool_response", "tool_output",
    "transcript_path", "permission_mode", "cwd",
]


def load(capture_dir: str) -> list[dict]:
    records = []
    for path in glob.glob(os.path.join(capture_dir, "*", "*.json")):
        try:
            with open(path) as f:
                records.append(json.load(f))
        except Exception:
            pass
    records.sort(key=lambda r: r.get("captured_at", 0))
    return records


def flatten(obj, prefix=""):
    """Yield (dotted_path, leaf_value) for every leaf in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flatten(v, f"{prefix}{k}.")
    elif isinstance(obj, list):
        yield (prefix.rstrip("."), f"[list x{len(obj)}]")
        if obj:
            yield from flatten(obj[0], f"{prefix}0.")
    else:
        yield (prefix.rstrip("."), obj)


def payload_of(rec: dict) -> dict:
    p = rec.get("stdin_parsed")
    return p if isinstance(p, dict) else {}


def main() -> None:
    capture_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR
    records = load(capture_dir)
    if not records:
        print(f"No captures under {capture_dir}")
        print("Enable capture (examples/claude-code.settings.capture.json), run a")
        print("Claude Code session, then re-run this.")
        return

    by_event: dict[str, list] = defaultdict(list)
    for r in records:
        by_event[r.get("event", "unknown")].append(r)

    print(f"# Capture report — {len(records)} events, {len(by_event)} types")
    print(f"# dir: {capture_dir}\n")

    # 1. Per-event payload schema
    print("## Per-event payload keys\n")
    for ev in sorted(by_event):
        recs = by_event[ev]
        keys = set()
        for r in recs:
            keys.update(path for path, _ in flatten(payload_of(r)))
        print(f"{ev}  (x{len(recs)})")
        for k in sorted(keys):
            print(f"    {k}")
        print()

    # 2. Tool-call correlation — the key M0 question
    print("## Tool-call correlation\n")
    id_fields: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for r in records:
        ev = r.get("event")
        if ev not in TOOL_EVENTS:
            continue
        for path, val in flatten(payload_of(r)):
            if "id" in path.lower() and not isinstance(val, (dict, list)):
                id_fields[path][ev].add(str(val))

    if not id_fields:
        print("  No id-like fields on tool events.")
        print("  -> correlate via transcript parsing or (tool_name, hash(input)).")
    else:
        # A per-call key varies per tool call: many distinct values, each
        # appearing in both Pre and Post. A constant like session_id is shared
        # but has a single value across all calls — not a correlation key.
        for field in sorted(id_fields):
            evs = id_fields[field]
            pre = evs.get("PreToolUse", set())
            post = evs.get("PostToolUse", set()) | evs.get("PostToolUseFailure", set())
            shared = pre & post
            if pre and post and len(shared) > 1:
                tag = f"  <- PER-CALL KEY ✓ ({len(shared)} distinct values shared Pre/Post)"
            elif pre and post and len(shared) == 1:
                tag = "  (single shared value — constant per session, not per-call)"
            elif pre and post:
                tag = "  (on both, but no shared values)"
            else:
                tag = ""
            print(f"  {field}: {sorted(evs)}{tag}")
    print()

    # 3. Field-path hints for the profile
    print("## Field-path hints (verify, then set in profile [fields])\n")
    all_paths = set()
    for r in records:
        all_paths.update(path for path, _ in flatten(payload_of(r)))
    for w in WANTED_FIELDS:
        hits = sorted(p for p in all_paths if p == w or p.endswith("." + w))
        print(f"  {w:16} -> {hits or '(not found)'}")


if __name__ == "__main__":
    main()
