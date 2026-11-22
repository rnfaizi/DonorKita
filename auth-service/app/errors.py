"""Envelope error klaster: {"error": {"code", "message", "request_id"}} (AGENTS.md bagian 3).

Kode yang dipakai:
  invalid_api_key 401, invalid_token 401, invalid_credentials 401, forbidden 403,
  not_found 404, conflict 409, validation_error 422, rate_limited 429,
  upstream_unavailable 503.
internal_error 500 belum ada di AGENTS.md; dipakai sebagai jaring pengaman dan
perlu diusulkan ke owner (lihat README bagian "Perubahan kontrak").
"""
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("donorkita")

DEFAULT_CODE = {
    401: "invalid_token",
    403: "forbidden",
    404: "not_found",
    405: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
    503: "upstream_unavailable",
}

DEFAULT_MESSAGE = {
    401: "Autentikasi diperlukan",
    403: "Akses ditolak",
    404: "Data tidak ditemukan",
    405: "Data tidak ditemukan",
    409: "Terjadi konflik data",
    422: "Input tidak valid",
    429: "Terlalu banyak permintaan",
    503: "Layanan sumber sedang tidak tersedia",
}


class ApiError(Exception):
    """Lempar dari kode domain/route untuk error dengan kode yang jelas."""

    def __init__(self, status: int, code: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers or {}


def request_id_of(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    return rid or request.headers.get("x-request-id") or uuid.uuid4().hex


def error_response(request: Request, status: int, code: str, message: str,
                   headers: dict | None = None) -> JSONResponse:
    rid = request_id_of(request)
    hdrs = {"X-Request-ID": rid, **(headers or {})}
    return JSONResponse({"error": {"code": code, "message": message, "request_id": rid}},
                        status_code=status, headers=hdrs)


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", request.state.request_id)
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return error_response(request, exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        status = 404 if exc.status_code == 405 else exc.status_code
        code = DEFAULT_CODE.get(exc.status_code, "internal_error" if status >= 500 else "validation_error")
        detail = exc.detail if isinstance(exc.detail, str) and exc.detail not in ("Not Found", "Method Not Allowed") else None
        message = detail or DEFAULT_MESSAGE.get(exc.status_code, "Terjadi kesalahan")
        return error_response(request, status, code, message, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        parts = [f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors()[:5]]
        return error_response(request, 422, "validation_error", "; ".join(parts) or "Input tidak valid")

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        log.exception("unhandled error request_id=%s", request_id_of(request))
        return error_response(request, 500, "internal_error", "Terjadi kesalahan di server")
