"""Minimal client for the CoreWeave Evaluations (CES) API and the W&B project lookup.

Standard library only: the sidecar must not gain dependencies for one JSON POST.
"""
from __future__ import annotations

import base64
import email.utils
import http.client
import json
import netrc
import os
import time
import urllib.error
import urllib.parse
import urllib.request

RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER_S = 60.0
_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends the Authorization header to a redirect target; the key stays on its host."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class CesError(Exception):
    """A CES request that failed. `status` is None for network errors and timeouts."""

    def __init__(self, status, code, message, retry_after=None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after
        self.attempts = 1

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUSES or (self.status is None and self.code == "network_error")


def api_key_for(wandb_base_url: str, netrc_path: str = None) -> str:
    """The API key `~/.netrc` holds for this W&B host. Never falls back to WANDB_API_KEY,
    which holds the production key in the harness environment."""
    host = urllib.parse.urlparse(wandb_base_url).hostname
    path = netrc_path or os.path.expanduser("~/.netrc")
    try:
        # `.hosts`, not `.authenticators()`, which falls back to the `default` entry.
        entry = netrc.netrc(path).hosts.get(host) if os.path.exists(path) else None
    except (netrc.NetrcParseError, OSError) as error:
        raise CesError(None, "netrc_error", f"{path}: {error}") from None
    if not entry or not entry[2]:
        raise CesError(None, "missing_api_key", f"no ~/.netrc entry for {host}")
    return entry[2]


def _basic_auth(api_key: str) -> str:
    return "Basic " + base64.b64encode(f"api:{api_key}".encode()).decode()


def _retry_after(value) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            return max(0.0, parsed.timestamp() - time.time())
        except (TypeError, ValueError, IndexError):
            return None


class CesClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0,
                 attempts: int = 3, backoff: tuple = (1.0, 4.0), sleep=time.sleep):
        self.base_url = base_url.rstrip("/")
        self.timeout, self.attempts, self.backoff, self.sleep = timeout, attempts, backoff, sleep
        self._auth = _basic_auth(api_key)

    def post(self, path: str, body, idempotency_key: str | None = None):
        return self.request("POST", path, body, idempotency_key)

    def patch(self, path: str, body, idempotency_key: str | None = None):
        return self.request("PATCH", path, body, idempotency_key)

    def request(self, method: str, path: str, body, idempotency_key: str | None = None):
        """Send with retries for network errors, 429, and 5xx. Raises the last CesError."""
        for attempt in range(1, self.attempts + 1):
            try:
                return self._once(method, path, body, idempotency_key)
            except CesError as error:
                if not error.retryable or attempt == self.attempts:
                    error.attempts = attempt
                    raise
                delay = self.backoff[min(attempt - 1, len(self.backoff) - 1)]
                if error.retry_after is not None:
                    delay = min(max(delay, error.retry_after), MAX_RETRY_AFTER_S)
                self.sleep(delay)

    def _once(self, method, path, body, idempotency_key):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            raw = error.read()
            error.close()
            try:
                envelope = json.loads(raw).get("error")
            except (ValueError, AttributeError):
                envelope = None
            if not isinstance(envelope, dict):
                envelope = {}
            raise CesError(error.code, envelope.get("code"), envelope.get("message") or error.reason,
                           _retry_after(error.headers.get("Retry-After"))) from None
        except _NETWORK_ERRORS as error:
            raise CesError(None, "network_error", f"{type(error).__name__}: {error}") from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise CesError(status, "invalid_response", "response body is not JSON") from None


def project_internal_id(wandb_base_url: str, api_key: str, entity: str, project: str,
                        timeout: float = 30.0) -> str:
    """The opaque project ID CES uses as `scope_ref`."""
    query = "query($e: String!, $p: String!) { project(entityName: $e, name: $p) { internalId } }"
    body = json.dumps({"query": query, "variables": {"e": entity, "p": project}}).encode()
    request = urllib.request.Request(
        wandb_base_url.rstrip("/") + "/graphql", data=body, method="POST",
        headers={"Authorization": _basic_auth(api_key), "Content-Type": "application/json"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        error.close()
        raise CesError(error.code, "graphql_error", error.reason) from None
    except _NETWORK_ERRORS as error:
        raise CesError(None, "network_error", f"{type(error).__name__}: {error}") from None
    except ValueError:
        raise CesError(None, "invalid_response", "response body is not JSON") from None
    if not isinstance(result, dict):
        raise CesError(None, "invalid_response", "response body is not a JSON object")
    if result.get("errors") and not result.get("data"):
        first = result["errors"][0]
        message = first.get("message") if isinstance(first, dict) else first
        raise CesError(None, "graphql_error", str(message))
    found = ((result.get("data") or {}).get("project") or {}).get("internalId")
    if not found:
        raise CesError(None, "project_not_found", f"{entity}/{project} not found or not visible")
    return found
