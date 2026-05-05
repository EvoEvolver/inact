from __future__ import annotations

import httpx

from inact import Inact, mount_remote_inact


class _FakeHttpClient:
    requests: list[dict] = []
    response = httpx.Response(200, content=b"ok\n", headers={"Content-Type": "text/plain"})
    exc: Exception | None = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        if self.exc:
            raise self.exc
        return self.response


def _reset_fake_client():
    _FakeHttpClient.requests = []
    _FakeHttpClient.response = httpx.Response(
        200,
        content=b"ok\n",
        headers={"Content-Type": "text/plain"},
    )
    _FakeHttpClient.exc = None


def test_remote_route_proxies_get(monkeypatch):
    _reset_fake_client()
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    app = Inact("test-remote-get")

    mount_remote_inact(app, "/chem", "http://harness/chem")

    response = app.app.test_client().get("/chem/tools?limit=2")

    assert response.status_code == 200
    assert response.text == "ok\n"
    assert _FakeHttpClient.requests[0]["method"] == "GET"
    assert _FakeHttpClient.requests[0]["url"] == "http://harness/chem/tools"
    assert _FakeHttpClient.requests[0]["params"]["limit"] == "2"


def test_remote_route_proxies_post_body_and_token(monkeypatch):
    _reset_fake_client()
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    app = Inact("test-remote-post")

    mount_remote_inact(app, "/chem", "http://harness/chem", token="secret")

    response = app.app.test_client().post(
        "/chem/tools/foo?x=1",
        json={"value": 42},
        headers={"Accept": "text/plain"},
    )

    proxied = _FakeHttpClient.requests[0]
    assert response.status_code == 200
    assert proxied["method"] == "POST"
    assert proxied["url"] == "http://harness/chem/tools/foo"
    assert proxied["params"]["x"] == "1"
    assert proxied["content"] == b'{"value": 42}'
    assert proxied["headers"]["Accept"] == "text/plain"
    assert proxied["headers"]["Content-Type"] == "application/json"
    assert proxied["headers"]["X-ElAgenteHarness-Token"] == "secret"
    assert "Host" not in proxied["headers"]


def test_remote_human_route_uses_human_upstream(monkeypatch):
    _reset_fake_client()
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    app = Inact("test-remote-human")

    mount_remote_inact(app, "/chem", "http://harness/chem")

    response = app.app.test_client().get("/_human/chem/tools")

    assert response.status_code == 200
    assert _FakeHttpClient.requests[0]["url"] == "http://harness/_human/chem/tools"


def test_remote_human_route_preserves_base_url_parent_path(monkeypatch):
    _reset_fake_client()
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    app = Inact("test-remote-human-parent")

    mount_remote_inact(app, "/chem", "http://harness/api/chem")

    response = app.app.test_client().get("/_human/chem/tools")

    assert response.status_code == 200
    assert _FakeHttpClient.requests[0]["url"] == "http://harness/api/_human/chem/tools"


def test_remote_mount_help_is_discoverable(monkeypatch):
    _reset_fake_client()
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    app = Inact("test-remote-help")

    mount_remote_inact(app, "/chem", "http://harness/chem")

    response = app.app.test_client().get("/.help")

    assert response.status_code == 200
    assert "Remote Inact app: /chem" in response.text
    assert "http://harness/chem" in response.text


def test_remote_upstream_failure_returns_502(monkeypatch):
    _reset_fake_client()
    _FakeHttpClient.exc = httpx.ConnectError("connection refused")
    monkeypatch.setattr(httpx, "Client", _FakeHttpClient)
    app = Inact("test-remote-failure")

    mount_remote_inact(app, "/chem", "http://harness/chem")

    response = app.app.test_client().get("/chem/tools")

    assert response.status_code == 502
    assert "ERROR 502: upstream request failed" in response.text
