from __future__ import annotations

import socket

import pytest

from app.security.urls import FetchError, FetchLimits, UnsafeURLError, normalize_url, safe_fetch


class FakeSocket:
    def settimeout(self, _value):
        pass


class FakeResponse:
    def __init__(self, status=200, body=b"<html>content</html>", content_type="text/html", location=None):
        self.status = status
        self._body = body
        self._content_type = content_type
        self._location = location
    def getheader(self, name, default=None):
        return {"Content-Type": self._content_type,
                "Content-Length": str(len(self._body)) if self._body else None,
                "Location": self._location}.get(name, default)
    def read(self, size):
        return self._body[:size]
    def close(self):
        pass


class FakeConnection:
    def __init__(self, response):
        self.sock = FakeSocket()
        self.response = response
    def request(self, *_args, **_kwargs):
        pass
    def getresponse(self):
        return self.response
    def close(self):
        pass


def test_normalization_and_reject_unsafe_url_forms():
    assert normalize_url("HTTPS://Example.COM/path#fragment") == "https://example.com/path"
    for url in ("file:///etc/passwd", "http://user:pass@example.com", "gopher://example.com"):
        with pytest.raises(UnsafeURLError):
            normalize_url(url)


def test_private_ip_and_invalid_dns_are_rejected():
    limits = FetchLimits()
    with pytest.raises(UnsafeURLError):
        safe_fetch("http://127.0.0.1/", limits)
    with pytest.raises(UnsafeURLError):
        safe_fetch("http://example.invalid/", limits, resolver=lambda *_a, **_k: [])
    with pytest.raises(UnsafeURLError):
        safe_fetch("http://example.com/", limits,
                   resolver=lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80))])


def test_fetch_parses_controlled_mock_response(monkeypatch):
    import app.security.urls as urls
    response = FakeResponse(body=b"<html>controlled local response</html>")
    monkeypatch.setattr(urls, "_connection", lambda *_a, **_k: FakeConnection(response))
    resolver = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    result = safe_fetch("http://example.com/article", FetchLimits(), resolver=resolver)
    assert result.status == 200
    assert b"controlled local response" in result.body


def test_redirect_to_private_destination_is_blocked(monkeypatch):
    import app.security.urls as urls
    first = FakeConnection(FakeResponse(status=302, location="http://127.0.0.1/admin", body=b""))
    monkeypatch.setattr(urls, "_connection", lambda *_a, **_k: first)
    resolver = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    with pytest.raises(UnsafeURLError):
        safe_fetch("http://example.com/article", FetchLimits(), resolver=resolver)


def test_size_and_content_type_limits(monkeypatch):
    import app.security.urls as urls
    resolver = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(urls, "_connection", lambda *_a, **_k: FakeConnection(FakeResponse(body=b"x" * 2000)))
    with pytest.raises(FetchError, match="size"):
        safe_fetch("http://example.com/", FetchLimits(max_bytes=1024), resolver=resolver)
    monkeypatch.setattr(urls, "_connection", lambda *_a, **_k: FakeConnection(FakeResponse(content_type="application/octet-stream")))
    with pytest.raises(FetchError, match="content type"):
        safe_fetch("http://example.com/", FetchLimits(), resolver=resolver)


def test_redirect_limit_and_timeout_are_bounded(monkeypatch):
    import app.security.urls as urls
    redirects = iter([
        FakeConnection(FakeResponse(status=302, location="http://redirect.example/one", body=b"")),
        FakeConnection(FakeResponse(status=302, location="http://redirect.example/two", body=b"")),
    ])
    monkeypatch.setattr(urls, "_connection", lambda *_a, **_k: next(redirects))
    resolver = lambda *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
    with pytest.raises(FetchError, match="Redirect limit"):
        safe_fetch("http://example.com/", FetchLimits(max_redirects=1), resolver=resolver)
    class TimeoutConnection(FakeConnection):
        def getresponse(self):
            raise TimeoutError("controlled timeout")
    monkeypatch.setattr(urls, "_connection", lambda *_a, **_k: TimeoutConnection(FakeResponse()))
    with pytest.raises(FetchError, match="connect"):
        safe_fetch("http://example.com/", FetchLimits(), resolver=resolver)


def test_dns_resolution_timeout_fails_closed():
    import time
    def slow_resolver(*_args, **_kwargs):
        time.sleep(0.1)
        return []
    with pytest.raises(UnsafeURLError, match="resolved"):
        safe_fetch("http://example.com/", FetchLimits(connect_timeout=0.01), resolver=slow_resolver)
