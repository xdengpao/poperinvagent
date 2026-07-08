"""HTTP 传输层：可注入（契约测试用 FakeTransport），默认走系统代理（HTTPS_PROXY）。

联测分层（任务 4 联测口径）：
- 契约测试：注入 FakeTransport，验证请求构造、解析、错误→UpstreamDown/Gap 语义，处处可跑；
- 真联测：RUN_INTEGRATION=1 且网络策略放行时启用（生产/CI），本沙箱网关 403 CONNECT 时自动跳过。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from .base import RateLimitPolicy, UpstreamDown


@dataclass
class HttpResponse:
    status: int
    body: bytes

    def json(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


class HttpTransport(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                data: bytes | None = None, timeout: float = 15.0) -> HttpResponse: ...


class UrllibTransport:
    """默认实现：urllib + 系统代理；带指数退避重试（RateLimitPolicy）。"""

    def __init__(self, rate_limit: RateLimitPolicy | None = None):
        self._policy = rate_limit or RateLimitPolicy()

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                data: bytes | None = None, timeout: float = 15.0) -> HttpResponse:
        last_err: Exception | None = None
        for attempt, backoff in enumerate((0.0, *self._policy.retry_backoff_s)):
            if backoff:
                time.sleep(backoff)
            req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                    return HttpResponse(status=resp.status, body=resp.read())
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < len(self._policy.retry_backoff_s):
                    last_err = e
                    continue
                return HttpResponse(status=e.code, body=e.read() or b"")
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                continue
        raise UpstreamDown(f"{url}: {last_err}")


def urlencode(params: dict) -> str:
    return urllib.parse.urlencode(params)
