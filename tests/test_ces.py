"""CES client retries, error envelopes, and per-host credentials."""
from __future__ import annotations

import pytest

from weave_agent_adapter import ces
from http_fake import FakeCes


@pytest.fixture
def fake():
    server = FakeCes()
    yield server
    server.close()


def _error(code):
    return {"error": {"code": code, "message": code}}


def test_retries_5xx_honoring_retry_after_then_succeeds(fake):
    sleeps = []
    fake.script = [(503, {"Retry-After": "2"}, _error("unavailable")), (200, {}, {"case_ids": ["c"]})]
    client = ces.CesClient(fake.url, "secret", sleep=sleeps.append)

    assert client.post("/rows", {"rows": []}, "key-1") == {"case_ids": ["c"]}
    assert sleeps == [2.0]
    path, headers, body = fake.requests[0]
    assert (path, headers["Idempotency-Key"], body) == ("/rows", "key-1", {"rows": []})
    assert headers["Authorization"] == "Basic YXBpOnNlY3JldA=="      # base64("api:secret")


def test_client_error_is_not_retried(fake):
    fake.script = [(400, {}, _error("invalid_request"))]
    client = ces.CesClient(fake.url, "k", sleep=lambda _: pytest.fail("must not sleep"))

    with pytest.raises(ces.CesError) as raised:
        client.post("/rows", {}, "key")
    assert (raised.value.status, raised.value.code, raised.value.attempts) == (400, "invalid_request", 1)


def test_gives_up_after_three_attempts(fake):
    fake.script = [(502, {}, _error("bad_gateway"))] * 3
    sleeps = []
    with pytest.raises(ces.CesError) as raised:
        ces.CesClient(fake.url, "k", sleep=sleeps.append).post("/rows", {}, "key")
    assert raised.value.attempts == 3 and sleeps == [1.0, 4.0]


def test_network_error_is_retryable():
    client = ces.CesClient("http://127.0.0.1:9", "k", timeout=0.5, attempts=1)
    with pytest.raises(ces.CesError) as raised:
        client.post("/rows", {}, "key")
    assert raised.value.status is None and raised.value.retryable


def test_api_key_comes_only_from_netrc_for_that_host(tmp_path, monkeypatch):
    monkeypatch.setenv("WANDB_API_KEY", "prod-key")
    path = tmp_path / "netrc"
    path.write_text("machine api.wandb.ai login user password prod\n"
                    "machine api.qa.wandb.ai login user password qa-key\n")
    path.chmod(0o600)

    assert ces.api_key_for("https://api.qa.wandb.ai", str(path)) == "qa-key"
    with pytest.raises(ces.CesError) as raised:
        ces.api_key_for("https://api.other.test", str(path))
    assert raised.value.code == "missing_api_key"


def test_netrc_default_entry_is_not_used(tmp_path):
    path = tmp_path / "netrc"
    path.write_text("default login u password PROD\n"
                    "machine api.qa.wandb.ai login u password qa-key\n")
    path.chmod(0o600)

    assert ces.api_key_for("https://api.qa.wandb.ai", str(path)) == "qa-key"
    with pytest.raises(ces.CesError) as raised:
        ces.api_key_for("https://api.other.test", str(path))
    assert raised.value.code == "missing_api_key"


def test_missing_and_malformed_netrc_are_typed_errors(tmp_path):
    with pytest.raises(ces.CesError) as missing:
        ces.api_key_for("https://api.qa.wandb.ai", str(tmp_path / "absent"))
    assert missing.value.code == "missing_api_key"

    bad = tmp_path / "netrc"
    bad.write_text("machine api.qa.wandb.ai bogus x\n")
    bad.chmod(0o600)
    with pytest.raises(ces.CesError) as malformed:
        ces.api_key_for("https://api.qa.wandb.ai", str(bad))
    assert malformed.value.code == "netrc_error"


def test_only_network_errors_and_retryable_statuses_are_retryable():
    assert not ces.CesError(None, "missing_api_key", "m").retryable
    assert not ces.CesError(None, "project_not_found", "m").retryable
    assert not ces.CesError(200, "invalid_response", "m").retryable
    assert ces.CesError(None, "network_error", "m").retryable
    assert ces.CesError(503, None, "m").retryable


def test_unparseable_retry_after_falls_back_to_backoff(fake):
    sleeps = []
    fake.script = [(503, {"Retry-After": "soon"}, _error("unavailable")), (200, {}, {})]
    ces.CesClient(fake.url, "k", sleep=sleeps.append).post("/rows", {}, "key")
    assert sleeps == [1.0]


def test_non_object_error_envelope_is_a_typed_error(fake):
    fake.script = [(400, {}, {"error": "x"})]
    with pytest.raises(ces.CesError) as raised:
        ces.CesClient(fake.url, "k").post("/rows", {}, "key")
    assert (raised.value.status, raised.value.code) == (400, None)


def test_non_json_success_body_is_invalid_response(fake):
    fake.script = [(200, {}, b"<html>proxy</html>")]
    with pytest.raises(ces.CesError) as raised:
        ces.CesClient(fake.url, "k", sleep=lambda _: pytest.fail("must not retry")).post("/rows", {}, "key")
    assert (raised.value.status, raised.value.code, raised.value.retryable) == (200, "invalid_response", False)


def test_network_error_message_names_the_exception():
    with pytest.raises(ces.CesError) as raised:
        ces.CesClient("http://127.0.0.1:9", "k", timeout=0.5, attempts=1).post("/rows", {}, "key")
    assert raised.value.code == "network_error" and raised.value.message.startswith("URLError: ") and "refused" in raised.value.message


def test_redirect_is_an_error_and_never_followed(fake):
    other = FakeCes()
    try:
        fake.script = [(302, {"Location": other.url + "/elsewhere"}, {})]
        with pytest.raises(ces.CesError) as raised:
            ces.CesClient(fake.url, "k", sleep=lambda _: pytest.fail("must not retry")).post("/rows", {}, "key")
        assert raised.value.status == 302
        assert len(fake.requests) == 1 and other.requests == []
    finally:
        other.close()


def test_project_internal_id_returns_the_id(fake):
    fake.script = [(200, {}, {"data": {"project": {"internalId": "UHJvamVjdDox"}}})]

    assert ces.project_internal_id(fake.url, "k", "ent", "proj") == "UHJvamVjdDox"
    path, _, body = fake.requests[0]
    assert path == "/graphql" and body["variables"] == {"e": "ent", "p": "proj"}


def test_project_internal_id_missing_project(fake):
    fake.script = [(200, {}, {"data": {"project": None}})]
    with pytest.raises(ces.CesError) as raised:
        ces.project_internal_id(fake.url, "k", "ent", "proj")
    assert raised.value.code == "project_not_found" and not raised.value.retryable


def test_project_internal_id_graphql_errors(fake):
    fake.script = [(200, {}, {"errors": [{"message": "boom"}, {"message": "later"}]})]
    with pytest.raises(ces.CesError) as raised:
        ces.project_internal_id(fake.url, "k", "ent", "proj")
    assert (raised.value.code, raised.value.message) == ("graphql_error", "boom")


def test_project_internal_id_non_json_and_network_failures(fake):
    fake.script = [(200, {}, b"not json")]
    with pytest.raises(ces.CesError) as raised:
        ces.project_internal_id(fake.url, "k", "ent", "proj")
    assert raised.value.code == "invalid_response"

    with pytest.raises(ces.CesError) as raised:
        ces.project_internal_id("http://127.0.0.1:9", "k", "ent", "proj", timeout=0.5)
    assert raised.value.code == "network_error"


def test_project_internal_id_does_not_follow_redirects(fake):
    other = FakeCes()
    try:
        fake.script = [(302, {"Location": other.url + "/graphql"}, {})]
        with pytest.raises(ces.CesError) as raised:
            ces.project_internal_id(fake.url, "k", "ent", "proj")
        assert raised.value.status == 302 and other.requests == []
    finally:
        other.close()
