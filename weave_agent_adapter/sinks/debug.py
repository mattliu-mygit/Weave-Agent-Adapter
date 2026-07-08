"""File sink: writes the finished trace tree on flush, local inspection, no Weave."""
from __future__ import annotations

from .recording import RecordingSink


class DebugSink(RecordingSink):
    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def flush(self) -> None:
        starts = {c.id: c for k, c in self.events if k == "start"}
        ended = {c.id for k, c in self.events if k == "end"}
        end_attrs = {c.id: (c.attributes or {}) for k, c in self.events if k == "end"}
        children: dict = {}
        for c in starts.values():
            children.setdefault(c.parent_id, []).append(c)
        lines: list[str] = []

        def walk(cid: str, depth: int) -> None:
            c = starts[cid]
            a = end_attrs.get(cid, {}).get("weave_agent_adapter", {})
            tag = a.get("status") or a.get("decision") or a.get("steering_kind") or ""
            mark = "" if cid in ended else " (open)"
            lines.append("  " * depth + c.op_name + (f"  · {tag}" if tag else "") + mark)
            for ch in children.get(cid, []):
                walk(ch.id, depth + 1)

        for root in children.get(None, []):
            walk(root.id, 0)
        with open(self.path, "w") as f:
            f.write("\n".join(lines) + "\n")
