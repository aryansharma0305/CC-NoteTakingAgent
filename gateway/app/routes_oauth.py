"""Notion OAuth: the extension runs launchWebAuthFlow and sends us the code; we
hold the client secret, exchange it, and return the token without storing it."""

from __future__ import annotations

import base64

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from vidnotes_common.errors import ErrorCode

from .errors import ApiError
from .state import AppState, get_state

router = APIRouter(prefix="/oauth/notion")


class ExchangeBody(BaseModel):
    code: str
    redirect_uri: str | None = None


@router.get("/config")
async def oauth_config(st: AppState = Depends(get_state)):
    """Public bits the extension needs to start the flow (no secrets)."""
    s = st.settings
    return {
        "configured": bool(s.notion_client_id and s.notion_client_secret),
        "client_id": s.notion_client_id or None,
        "redirect_uri": s.notion_redirect_uri or None,
        "authorize_url": "https://api.notion.com/v1/oauth/authorize",
    }


@router.post("/exchange")
async def oauth_exchange(body: ExchangeBody, st: AppState = Depends(get_state)):
    s = st.settings
    if not (s.notion_client_id and s.notion_client_secret):
        raise ApiError(503, ErrorCode.OAUTH_NOT_CONFIGURED)
    redirect = body.redirect_uri or s.notion_redirect_uri
    basic = base64.b64encode(f"{s.notion_client_id}:{s.notion_client_secret}".encode()).decode()
    try:
        r = await st.http.post(
            "https://api.notion.com/v1/oauth/token",
            json={"grant_type": "authorization_code", "code": body.code, "redirect_uri": redirect},
            headers={"Authorization": f"Basic {basic}", "Notion-Version": s.notion_version},
            timeout=15,
        )
    except Exception as e:  # noqa: BLE001
        raise ApiError(502, ErrorCode.OAUTH_EXCHANGE_FAILED, f"Could not reach Notion: {type(e).__name__}") from e
    if r.status_code != 200:
        try:
            detail = r.json().get("error_description") or r.json().get("message") or r.text[:200]
        except ValueError:
            detail = r.text[:200]
        raise ApiError(400, ErrorCode.OAUTH_EXCHANGE_FAILED, f"Notion refused the code: {detail}")
    data = r.json()
    return {
        "access_token": data.get("access_token"),
        "workspace_name": data.get("workspace_name"),
        "workspace_id": data.get("workspace_id"),
        "bot_id": data.get("bot_id"),
        "duplicated_template_id": data.get("duplicated_template_id"),
    }
