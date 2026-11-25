"""Config-surface fingerprint (the A/B key for evaluation cohorts).

Hashes the deliberately-edited context artifacts that shape agent behavior —
CLAUDE.md, skills, commands — into a short stable id, stamped on every
turn so score cohorts can be compared before/after a config change. Which
paths make up the surface is harness-specific, declared in the profile's
[config_surface] section; ambient state that churns on its own (auto-memory)
is deliberately excluded so the cohort key stays stable, and the section's
optional `exclude` list drops paths under it that the harness rewrites itself.
Placeholders: `~` (home), `{cwd}` (session cwd),
`{cwd_slug}` (cwd with "/" -> "-", Claude Code's ~/.claude/projects naming).

Content-hashed (not mtimes) so the id is stable across restarts and machines.
Symlinked files and directories count by their target's content, under the
path the profile entry shows. Missing paths are skipped; if nothing exists at
all the version is None.
"""
from __future__ import annotations

import hashlib
import os

_SKIP = {".DS_Store", "__pycache__"}
_MAX_FILES = 1000            # runaway-directory guard
_MAX_FILE_BYTES = 1 << 20    # per-file read cap


def _expand(template: str, cwd):
    if "{cwd}" in template or "{cwd_slug}" in template:
        if not cwd:
            return None                       # cwd-relative entry, no cwd known
        template = (template.replace("{cwd}", cwd)
                            .replace("{cwd_slug}", cwd.replace("/", "-")))
    return os.path.normpath(os.path.expanduser(template))


def _files_of(path: str, excluded):
    if os.path.isfile(path):
        yield path
        return
    if not os.path.isdir(path):
        return
    n = 0
    walked = {os.path.realpath(path)}   # ends symlink loops; a second link to a walked dir is skipped
    for root, dirs, files in os.walk(path, followlinks=True):
        kept = []
        for d in sorted(dirs):                                # deterministic walk
            sub = os.path.join(root, d)
            real = os.path.realpath(sub)
            if d in _SKIP or sub in excluded or real in walked:
                continue
            walked.add(real)
            kept.append(d)
        dirs[:] = kept
        for name in sorted(files):
            fp = os.path.join(root, name)
            if name in _SKIP or name.endswith(".pyc") or fp in excluded:
                continue
            yield fp
            n += 1
            if n >= _MAX_FILES:
                return


def config_version(paths, cwd=None, exclude=None):
    excluded = {p for p in (_expand(str(t), cwd) for t in exclude or []) if p}
    h = hashlib.sha256()
    seen = False
    for template in paths or []:
        base = _expand(str(template), cwd)
        if not base:
            continue
        for fp in _files_of(base, excluded):
            try:
                with open(fp, "rb") as fh:
                    data = fh.read(_MAX_FILE_BYTES)
            except Exception:
                continue
            seen = True
            rel = os.path.relpath(fp, base) if fp != base else os.path.basename(fp)
            for part in (str(template).encode(), rel.encode(), data):
                h.update(part)
                h.update(b"\0")
    return h.hexdigest()[:12] if seen else None
