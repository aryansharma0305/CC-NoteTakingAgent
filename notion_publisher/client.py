"""Rate-limited httpx client for the Notion API.

Error mapping (so the worker's retry logic stays generic):
  429                    -> RateLimited("notion", Retry-After)
  5xx / timeout / network -> RetryableError(NOTION_ERROR)
  401                    -> FatalError(NOTION_UNAUTHORIZED)  ("Reconnect Notion")
  other 4xx              -> NotionAPIError (a FatalError carrying status + Notion code)
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from vidnotes_common.errors import ErrorCode, FatalError, RateLimited, RetryableError

NOTION_API = "https://api.notion.com/v1"

Acquire = Callable[[], Awaitable[None]]


class NotionAPIError(FatalError):
    def __init__(self, status: int, notion_code: str, message: str):
        self.status = status
        self.notion_code = notion_code
        code = {
            403: ErrorCode.NOTION_FORBIDDEN,
            404: ErrorCode.NOTION_PARENT_NOT_FOUND,
        }.get(status, ErrorCode.NOTION_BAD_REQUEST)
        super().__init__(code, f"Notion {status} {notion_code}: {message}"[:500])


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        version: str = "2022-06-28",
        acquire: Acquire | None = None,
        fault_429_rate: float = 0.0,
        http: httpx.AsyncClient | None = None,
        base_url: str = NOTION_API,
        timeout: float = 30.0,
    ):
        self._token = token
        self._version = version
        self._acquire = acquire
        self._fault = fault_429_rate
        self._base = base_url.rstrip("/")
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._own_http:
            await self._http.aclose()

    async def __aenter__(self) -> NotionClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    def _headers(self, json_body: bool) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._token}", "Notion-Version": self._version}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._acquire is not None:
            await self._acquire()
        if self._fault and random.random() < self._fault:
            raise RateLimited("notion", retry_after=None, message="Rate-limited by Notion (injected fault)")
        url = f"{self._base}/{path.lstrip('/')}"
        try:
            resp = await self._http.request(
                method, url, json=json, params=params, files=files, headers=self._headers(files is None)
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise RetryableError(ErrorCode.NOTION_ERROR, f"Notion network error: {type(e).__name__}") from e

        if resp.status_code < 300:
            return resp.json() if resp.content else {}
        try:
            body = resp.json()
        except ValueError:
            body = {}
        ncode = body.get("code", "") if isinstance(body, dict) else ""
        nmsg = body.get("message", resp.text[:200]) if isinstance(body, dict) else resp.text[:200]
        if resp.status_code == 429 or ncode == "rate_limited":
            raise RateLimited("notion", retry_after=_retry_after(resp), message="Rate-limited by Notion")
        if resp.status_code >= 500 or ncode in ("conflict_error", "service_unavailable", "internal_server_error"):
            raise RetryableError(ErrorCode.NOTION_ERROR, f"Notion {resp.status_code}: {nmsg}"[:300])
        if resp.status_code == 401:
            raise FatalError(ErrorCode.NOTION_UNAUTHORIZED)
        raise NotionAPIError(resp.status_code, ncode, nmsg)

    # ---- thin helpers -------------------------------------------------
    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self.request("GET", path, params=params or None)

    async def post(self, path: str, body: Any) -> dict[str, Any]:
        return await self.request("POST", path, json=body)

    async def patch(self, path: str, body: Any) -> dict[str, Any]:
        return await self.request("PATCH", path, json=body)

    async def list_children(self, block_id: str):
        """Yield every child block of a page/block (paginated)."""
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = await self.request("GET", f"blocks/{block_id}/children", params=params)
            for b in data.get("results", []):
                yield b
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
