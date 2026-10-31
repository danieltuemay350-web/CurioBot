from __future__ import annotations

import socket
import ssl
from io import BytesIO

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
        self._offset = 0
    def getheader(self, name, default=None):
        return {"Content-Type": self._content_type,
                "Content-Length": str(len(self._body)) if self._body else None,
                "Location": self._location}.get(name, default)
    def read(self, size):
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk
    def read1(self, size):
        return self.read(size)
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


@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.2.3", "169.254.169.254", "224.0.0.1", "240.0.0.1",
    "0.0.0.0", "::1", "fc00::1", "fe80::1", "ff02::1", "2001:db8::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:224.0.0.1",
])
def test_dns_rejects_nonpublic_and_special_addresses(address):
    resolver = lambda *_args, **_kwargs: [
        (socket.AF_INET6 if ":" in address else socket.AF_INET,
         socket.SOCK_STREAM, 6, "", (address, 80))]
    with pytest.raises(UnsafeURLError):
        safe_fetch("http://address-test.invalid/", FetchLimits(), resolver=resolver)


def test_unusual_numeric_ipv4_hostname_is_checked_after_resolution():
    resolver = lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
    with pytest.raises(UnsafeURLError):
        safe_fetch("http://2130706433/", FetchLimits(), resolver=resolver)


class TransportSocket:
    """Socket seam that exercises stdlib HTTP framing without opening a network socket."""
    def __init__(self, response: bytes):
        self.response = response
        self.request_bytes = bytearray()
        self.timeout = None
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.request_bytes.extend(data)

    def makefile(self, _mode):
        return BytesIO(self.response)

    def close(self):
        self.closed = True


def _http_response(body=b"safe transport body", *, status=200, headers="Content-Type: text/html\r\n"):
    reason = {200: "OK", 302: "Found"}.get(status, "Response")
    return (f"HTTP/1.1 {status} {reason}\r\n{headers}Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n").encode() + body


def test_real_httpconnection_uses_pinned_ip_host_header_and_ignores_proxy_env(monkeypatch):
    import app.security.urls as urls
    transport = TransportSocket(_http_response())
    connect_calls = []

    def controlled_connect(address, timeout):
        connect_calls.append((address, timeout))
        return transport

    monkeypatch.setattr(urls.socket, "create_connection", controlled_connect)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9")
    resolver = lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 8080))]

    result = safe_fetch("http://transport.example:8080/article?q=1",
                        FetchLimits(allowed_ports=frozenset({8080})), resolver=resolver)

    assert result.body == b"safe transport body"
    assert connect_calls[0][0] == ("93.184.216.34", 8080)
    assert 0 < connect_calls[0][1] <= 5
    request = transport.request_bytes.decode("iso-8859-1")
    assert request.startswith("GET /article?q=1 HTTP/1.1\r\n")
    assert "Host: transport.example:8080\r\n" in request
    assert "Proxy-Authorization:" not in request
    assert transport.closed


def test_real_httpsconnection_keeps_hostname_for_tls_and_host_header(monkeypatch):
    import app.security.urls as urls
    transport = TransportSocket(_http_response())
    policy = ssl.create_default_context()
    calls = []

    class RecordingContext:
        check_hostname = policy.check_hostname
        verify_mode = policy.verify_mode

        def wrap_socket(self, raw, *, server_hostname):
            calls.append((raw, server_hostname))
            return transport

    monkeypatch.setattr(urls.ssl, "create_default_context", lambda: RecordingContext())
    connect_calls = []
    monkeypatch.setattr(urls.socket, "create_connection",
                        lambda address, timeout: (connect_calls.append((address, timeout)) or object()))
    resolver = lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 8443))]

    result = safe_fetch("https://secure.example:8443/article",
                        FetchLimits(allowed_ports=frozenset({8443})), resolver=resolver)

    assert result.body == b"safe transport body"
    assert connect_calls[0][0] == ("93.184.216.34", 8443)
    assert 0 < connect_calls[0][1] <= 5
    assert calls[0][1] == "secure.example"
    assert calls[0][0] is not None
    assert RecordingContext.check_hostname is True
    assert RecordingContext.verify_mode == ssl.CERT_REQUIRED
    assert "Host: secure.example:8443\r\n" in transport.request_bytes.decode("iso-8859-1")


def test_real_httpconnection_revalidates_redirect_and_creates_new_connection(monkeypatch):
    import app.security.urls as urls
    first = TransportSocket(_http_response(b"", status=302,
                                            headers="Content-Type: text/html\r\nLocation: http://next.example:8080/end\r\n"))
    second = TransportSocket(_http_response(b"final body"))
    responses = iter([first, second])
    connect_calls = []
    monkeypatch.setattr(urls.socket, "create_connection",
                        lambda address, timeout: (connect_calls.append(address) or next(responses)))
    resolved_hosts = []

    def resolver(host, port, **_kwargs):
        resolved_hosts.append((host, port))
        ip = "93.184.216.34" if host == "initial.example" else "1.1.1.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    result = safe_fetch("http://initial.example:8080/start",
                        FetchLimits(allowed_ports=frozenset({8080})), resolver=resolver)
    assert result.url == "http://next.example:8080/end"
    assert result.body == b"final body"
    assert resolved_hosts == [("initial.example", 8080), ("next.example", 8080)]
    assert connect_calls == [("93.184.216.34", 8080), ("1.1.1.1", 8080)]
    assert "Host: initial.example:8080\r\n" in first.request_bytes.decode("iso-8859-1")
    assert "Host: next.example:8080\r\n" in second.request_bytes.decode("iso-8859-1")


def test_real_httpconnection_never_opens_connection_for_private_redirect(monkeypatch):
    import app.security.urls as urls
    first = TransportSocket(_http_response(b"", status=302,
                                            headers="Content-Type: text/html\r\nLocation: http://127.0.0.1/admin\r\n"))
    connect_calls = []
    monkeypatch.setattr(urls.socket, "create_connection",
                        lambda address, timeout: (connect_calls.append(address) or first))
    resolver = lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 8080))]
    with pytest.raises(UnsafeURLError):
        safe_fetch("http://initial.example:8080/start",
                   FetchLimits(allowed_ports=frozenset({8080})), resolver=resolver)
    assert connect_calls == [("93.184.216.34", 8080)]


def test_response_read_has_a_total_deadline(monkeypatch):
    import app.security.urls as urls
    clock = [0.0]
    monkeypatch.setattr(urls.time, "monotonic", lambda: clock[0])

    class SlowDrip:
        def read1(self, _size):
            clock[0] += 0.6
            return b"x"

    class ReadSocket:
        def settimeout(self, timeout):
            self.last_timeout = timeout

    transport = ReadSocket()
    with pytest.raises(FetchError, match="deadline"):
        urls._read_bounded_body(SlowDrip(), transport, max_bytes=100, timeout=1.0)
    assert transport.last_timeout <= 1.0


def test_connection_deadline_covers_all_resolved_addresses(monkeypatch):
    import time
    import app.security.urls as urls
    attempted = []

    def slow_connect(address, _timeout):
        attempted.append(address)
        time.sleep(0.12)
        raise TimeoutError("controlled connect timeout")

    monkeypatch.setattr(urls.socket, "create_connection", slow_connect)
    resolver = lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 80)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80)),
    ]
    with pytest.raises(FetchError, match="Could not connect"):
        safe_fetch("http://multi-address.example/",
                   FetchLimits(connect_timeout=0.1), resolver=resolver)
    assert len(attempted) == 1
