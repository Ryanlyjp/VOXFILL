"""Small async client for the local FlareSolverr service."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


class FlareSolverError(RuntimeError):
    """Raised when FlareSolverr cannot return a usable browser solution."""


@dataclass(frozen=True)
class FlareSolverSolution:
    html: str
    cookies: list[dict[str, object]]
    user_agent: str
    url: str


def _endpoint() -> str:
    value = os.environ.get("VOXTRY_FLARESOLVERR_URL", "http://127.0.0.1:8191/v1").strip().rstrip("/")
    if not value:
        raise FlareSolverError("未配置 FlareSolverr 地址")
    return value if value.endswith("/v1") else f"{value}/v1"


def _timeout_ms() -> int:
    try:
        return max(1_000, int(os.environ.get("VOXTRY_FLARESOLVERR_TIMEOUT_MS", "180000")))
    except ValueError:
        return 180_000


def _proxy_payload(proxy: str | None) -> dict[str, str] | None:
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        raise FlareSolverError("FlareSolverr 代理格式无效")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    payload: dict[str, str] = {"url": f"{parsed.scheme}://{host}:{parsed.port}"}
    if parsed.username is not None:
        payload["username"] = unquote(parsed.username)
    if parsed.password is not None:
        payload["password"] = unquote(parsed.password)
    return payload


def _post_json(endpoint: str, payload: dict[str, object], timeout_seconds: float) -> dict[str, object]:
    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            raw_detail = exc.read().decode("utf-8", errors="replace")
            if raw_detail:
                parsed_detail = json.loads(raw_detail)
                if isinstance(parsed_detail, dict):
                    detail = str(parsed_detail.get("message") or raw_detail)
                else:
                    detail = raw_detail
        except (OSError, UnicodeError, json.JSONDecodeError):
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise FlareSolverError(f"FlareSolverr HTTP {exc.code}{suffix}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise FlareSolverError(f"FlareSolverr 请求失败：{type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise FlareSolverError("FlareSolverr 返回内容无效")
    return value


async def _post_json_async(
    endpoint: str, payload: dict[str, object], timeout_seconds: float
) -> dict[str, object]:
    return await asyncio.to_thread(_post_json, endpoint, payload, timeout_seconds)


def _cookies(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for raw in value:
        if not isinstance(raw, dict) or not raw.get("name") or raw.get("value") is None:
            continue
        domain = raw.get("domain")
        if not domain:
            continue
        item: dict[str, object] = {
            "name": str(raw["name"]),
            "value": str(raw["value"]),
            "domain": str(domain),
            "path": str(raw.get("path") or "/"),
        }
        if isinstance(raw.get("expires"), (int, float)) and raw["expires"] > 0:
            item["expires"] = raw["expires"]
        for key in ("httpOnly", "secure"):
            if isinstance(raw.get(key), bool):
                item[key] = raw[key]
        if raw.get("sameSite") in {"Strict", "Lax", "None"}:
            item["sameSite"] = raw["sameSite"]
        result.append(item)
    return result


async def solve_page(url: str, proxy: str | None = None) -> FlareSolverSolution:
    """Solve the page with FlareSolverr's Chromium stealth driver."""
    timeout_ms = _timeout_ms()
    response = await _post_json_async(
        _endpoint(),
        {
            "cmd": "request.get",
            "url": url,
            "proxy": _proxy_payload(proxy),
            "maxTimeout": timeout_ms,
            "returnOnlyCookies": False,
            "returnScreenshot": False,
            "waitInSeconds": 0,
        },
        timeout_ms / 1000 + 15,
    )
    if response.get("status") != "ok":
        raise FlareSolverError(str(response.get("message") or "FlareSolverr 未完成页面求解"))
    solution = response.get("solution")
    if not isinstance(solution, dict):
        raise FlareSolverError("FlareSolverr 返回内容缺少 solution")

    cookies = _cookies(solution.get("cookies"))
    html = str(solution.get("response") or "")
    if not html:
        raise FlareSolverError("FlareSolverr 未返回 HTML")
    return FlareSolverSolution(
        html=html,
        cookies=cookies,
        user_agent=str(solution.get("userAgent") or "").strip(),
        url=str(solution.get("url") or url),
    )
