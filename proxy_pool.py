"""Proxy parsing and batch-local random proxy selection."""
from __future__ import annotations

import secrets
import threading
from urllib.parse import quote, unquote, urlparse


def normalize_proxy(value: str) -> str:
    """Convert supported proxy input formats to a standard proxy URL."""
    value = value.strip()
    if not value:
        return ""
    if "://" in value:
        return value
    if "@" in value:
        return f"http://{value}"

    parts = value.split(":", 3)
    if len(parts) == 4 and parts[1].isdigit():
        host, port, username, password = parts
        return f"http://{quote(username)}:{quote(password)}@{host}:{port}"
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{value}"
    raise ValueError("代理格式无效")


def parse_proxy_pool(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        value = raw.strip()
        if value and not value.startswith("#"):
            proxy = normalize_proxy(value)
            if proxy not in seen:
                seen.add(proxy)
                result.append(proxy)
    return result


def to_playwright_proxy(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None
    parsed = urlparse(normalize_proxy(proxy))
    if not parsed.hostname:
        return None
    server = f"{parsed.scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    result: dict[str, str] = {"server": server}
    if parsed.username:
        result["username"] = unquote(parsed.username)
    if parsed.password:
        result["password"] = unquote(parsed.password)
    return result


def mask_proxy(proxy: str) -> str:
    parsed = urlparse(normalize_proxy(proxy))
    if not parsed.hostname:
        return proxy
    scheme = parsed.scheme or "http"
    value = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        value += f":{parsed.port}"
    return f"{value} (auth)" if parsed.username else value


class ProxyPool:
    def __init__(self, proxies: list[str], rotation: bool):
        self._lock = threading.RLock()
        self.rotation = rotation
        self._order = list(proxies)
        self._unused = list(proxies)
        self._blocked: set[str] = set()

    def acquire(self) -> str | None:
        with self._lock:
            available = [proxy for proxy in self._order if proxy not in self._blocked]
            if not available:
                return None
            if not self.rotation:
                return available[0]
            candidates = [proxy for proxy in self._unused if proxy in available]
            if not candidates:
                self._unused = list(available)
                candidates = self._unused
            proxy = secrets.choice(candidates)
            self._unused.remove(proxy)
            return proxy

    def block(self, proxy: str | None) -> None:
        if not proxy:
            return
        with self._lock:
            if proxy in self._order:
                self._blocked.add(proxy)
                self._unused = [item for item in self._unused if item != proxy]

    def release_blocked(self) -> None:
        with self._lock:
            self._blocked.clear()
            self._unused = list(self._order)

    def has_configured_proxies(self) -> bool:
        with self._lock:
            return bool(self._order)

    def is_blocked(self, proxy: str | None) -> bool:
        with self._lock:
            return bool(proxy and proxy in self._blocked)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "rotation": self.rotation,
                "size": len(self._order),
                "proxies": [mask_proxy(proxy) for proxy in self._order],
                "blocked": [mask_proxy(proxy) for proxy in self._blocked],
            }
