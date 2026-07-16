"""Scrub likely secrets before values leave the sidecar.

Two rules: (1) if a dict key looks sensitive, drop the whole value; (2) scrub
secret-shaped substrings inside any string. Applied in the tracer, so every output
(Weave, debug, …) receives already-redacted data. Best-effort, not a guarantee , 
tune the denylist/patterns for your environment.
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

DEFAULT_DENY_KEYS = {
    "api_key", "apikey", "authorization", "auth", "token", "access_token",
    "refresh_token", "password", "passwd", "secret", "secret_key", "access_key",
    "private_key", "client_secret", "cookie", "bearer", "credentials",
}

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),                       # OpenAI-style
    re.compile(r"wandb_v1_[A-Za-z0-9_\-]{20,}"),              # W&B key
    re.compile(r"gh[posru]_[A-Za-z0-9]{20,}"),                # GitHub token
    re.compile(r"AKIA[0-9A-Z]{16}"),                          # AWS access key id
    re.compile(r"eyJ[\w\-]{8,}\.[\w\-]{8,}\.[\w\-]{8,}"),     # JWT
    re.compile(
        r"-----BEGIN ([A-Z ]*PRIVATE KEY)-----.*?-----END \1-----",
        re.DOTALL,
    ),                                                        # full PEM block
]


class Redactor:
    def __init__(self, deny_keys=None, enabled: bool = True):
        keys = DEFAULT_DENY_KEYS | set(deny_keys) if deny_keys is not None else DEFAULT_DENY_KEYS
        self.deny = {k.lower() for k in keys}
        self.enabled = enabled

    def scrub(self, value: Any, key: str | None = None) -> Any:
        if not self.enabled:
            return value
        if key is not None and any(d in key.lower() for d in self.deny):
            return REDACTED
        if isinstance(value, dict):
            return {k: self.scrub(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub(v) for v in value]
        if isinstance(value, str):
            for pat in _PATTERNS:
                value = pat.sub(REDACTED, value)
        return value
