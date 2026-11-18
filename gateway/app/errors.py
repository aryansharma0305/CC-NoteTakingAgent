"""Uniform error responses: {"error": {"code", "message"}} for every 4xx/5xx."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from vidnotes_common.errors import ErrorCode, api_error


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None, **details: Any):
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError):
        return JSONResponse(status_code=e.status, content=api_error(e.code, e.message, **e.details))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        msg = f"{where}: {first.get('msg', 'invalid')}" if where else "Request body is invalid JSON."
        return JSONResponse(status_code=400, content=api_error(ErrorCode.INVALID_REQUEST, msg))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, e: StarletteHTTPException):
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(e.status_code, "HTTP_ERROR")
        return JSONResponse(status_code=e.status_code, content=api_error(code, str(e.detail)))
