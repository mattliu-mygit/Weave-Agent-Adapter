"""Name the repository a session ran in, so Scores can be grouped by it.

`repo` must be the same on every machine and must not reveal a local path:

1. the path of the `origin` remote's URL after the host, without credentials or a
   trailing `.git`: `git@github.com:wandb/core.git` and
   `https://token@github.com/wandb/core` both give `wandb/core`;
2. else, such as with no `origin` or one that is a local path, the basename of the
   working tree's top-level directory: `/Users/me/src/core` gives `core`;
3. else, outside a Git repository or without `git`, None.

It is lowercased, because GitHub ignores case: `Wandb/Core` and `wandb/core` are one repository.
"""
from __future__ import annotations

import functools
import os
import re
import subprocess
import urllib.parse

GIT_TIMEOUT_S = 2.0
# Git reads `host:path` as SSH when no slash comes before the colon: `git@github.com:o/r.git`.
_SCP_LIKE = re.compile(r"^(?:[^@/:]+@)?[^/:]+:(?P<path>.+)$")


# The sidecar asks once per working directory for its lifetime, which ends when it goes
# idle, so a remote changed meanwhile shows from the next sidecar on.
@functools.lru_cache(maxsize=256)
def repo_identity(cwd: str | None) -> str | None:
    if not cwd:
        return None
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top is None:
        return None
    remote = _git(cwd, "remote", "get-url", "origin")
    return ((remote and remote_path(remote)) or os.path.basename(top)).lower() or None


def remote_path(url: str) -> str | None:
    """The repository path in a hosted remote URL, or None for a local path."""
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:                  # file:///srv/git/core.git
            return None
        path = parts.path
    else:
        match = _SCP_LIKE.match(url)
        if match is None:                       # /srv/git/core.git or ../core
            return None
        path = match.group("path")
    return path.strip("/").removesuffix(".git").rstrip("/") or None


def _git(cwd: str, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and out else None
