"""Safe URL normalization, DNS validation, and bounded fetching."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

_DNS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="safe-dns")


class UnsafeURLError(ValueError):
    """URL or destination is not safe to fetch."""


class FetchError(RuntimeError):
    """The fetch failed within configured limits."""


@dataclass(frozen=True)
class FetchLimits:
    connect_timeout: float = 5
    read_timeout: float = 10
    max_bytes: int = 2_000_000
    max_redirects: int = 3
    allowed_ports: frozenset[int] = frozenset({80, 443})


@dataclass(frozen=True)
class FetchedResponse:
    url: str
    status: int
    content_type: str
    body: bytes


def normalize_url(url: str) -> str:
    if len(url) > 2048:
        raise UnsafeURLError("URL exceeds the supported length")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeURLError("Only HTTP and HTTPS URLs are supported")
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise UnsafeURLError("URL must have a host and must not contain credentials")
    try:
        port = parts.port
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError) as exc:
        raise UnsafeURLError("Malformed URL host or port") from exc
    if not host or any(c in host for c in "\\%\x00"):
        raise UnsafeURLError("Malformed URL host")
    netloc = f"[{host}]" if ":" in host else host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def resolve_public_addresses(host: str, port: int, resolver=socket.getaddrinfo,
                             timeout: float = 5) -> tuple[str, ...]:
    try:
        records = _DNS_POOL.submit(resolver, host, port, type=socket.SOCK_STREAM).result(timeout=timeout)
    except (OSError, FutureTimeout) as exc:
        raise UnsafeURLError("Hostname could not be resolved") from exc
    addresses: set[str] = set()
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0].split("%", 1)[0])
        except ValueError as exc:
            raise UnsafeURLError("DNS returned an invalid address") from exc
        if not address.is_global:
            raise UnsafeURLError("Host resolves to a non-public IP address")
        addresses.add(str(address))
    if not addresses:
        raise UnsafeURLError("Hostname returned no addresses")
    return tuple(sorted(addresses))


def _connection(url: str, ip: str, timeout: float) -> http.client.HTTPConnection:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if parts.scheme == "https":
        return _PinnedHTTPSConnection(parts.hostname or "", ip, port, timeout)
    return _PinnedHTTPConnection(parts.hostname or "", ip, port, timeout)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def safe_fetch(
    url: str, limits: FetchLimits, *, resolver=socket.getaddrinfo
) -> FetchedResponse:
    current = normalize_url(url)
    accepted = {"text/html", "application/xhtml+xml", "application/rss+xml", "application/feed+xml", "application/xml", "text/xml", "application/atom+xml"}
    for redirect_count in range(limits.max_redirects + 1):
        parts = urlsplit(current)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if port not in limits.allowed_ports:
            raise UnsafeURLError("Destination port is not allowed")
        host = parts.hostname or ""
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            addresses = resolve_public_addresses(host, port, resolver, limits.connect_timeout)
        else:
            if not literal.is_global:
                raise UnsafeURLError("Host is a non-public IP address")
            addresses = (str(literal),)
        # Pin the actual TCP connection to a validated address; do not let a second DNS
        # lookup change the destination between policy validation and connect.
        last_error: Exception | None = None
        response: FetchedResponse | None = None
        for address in addresses:
            conn = _connection(current, address, limits.connect_timeout)
            try:
                request_path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
                conn.request("GET", request_path, headers={
                    "Host": parts.netloc,
                    "User-Agent": "TG-Reading-Bot/0.1 (+safe article fetch)",
                    "Accept": ", ".join(sorted(accepted)),
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                assert conn.sock is not None
                conn.sock.settimeout(limits.read_timeout)
                raw = conn.getresponse()
                status = raw.status
                if status in {301, 302, 303, 307, 308}:
                    location = raw.getheader("Location")
                    raw.close()
                    conn.close()
                    if not location:
                        raise FetchError("Redirect had no destination")
                    if redirect_count >= limits.max_redirects:
                        raise FetchError("Redirect limit exceeded")
                    current = normalize_url(urljoin(current, location))
                    response = FetchedResponse(current, status, "", b"")
                    break
                content_type = raw.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
                content_length = raw.getheader("Content-Length")
                if content_length and int(content_length) > limits.max_bytes:
                    raise FetchError("Response exceeds the configured size limit")
                if status < 200 or status >= 300:
                    raise FetchError(f"Remote server returned HTTP {status}")
                if content_type not in accepted:
                    raise FetchError("Unsupported response content type")
                body = raw.read(limits.max_bytes + 1)
                if len(body) > limits.max_bytes:
                    raise FetchError("Response exceeds the configured size limit")
                response = FetchedResponse(current, status, content_type, body)
                raw.close()
                conn.close()
                break
            except (UnsafeURLError, FetchError):
                conn.close()
                raise
            except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
                conn.close()
                last_error = exc
        if response is None:
            raise FetchError("Could not connect to validated public destination") from last_error
        if response.status not in {301, 302, 303, 307, 308}:
            return response
    raise FetchError("Redirect limit exceeded")
