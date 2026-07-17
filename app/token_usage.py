from collections import defaultdict, deque
from pathlib import Path
from time import monotonic

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.router_tokens import RouterTokenClient, RouterTokenError, RouterTokenKeyNotFound


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


class UsageRateLimiter:
    def __init__(
        self,
        limit: int = 10,
        window_seconds: int = 60,
        max_clients: int = 5_000,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self.attempts: dict[str, deque[float]] = defaultdict(deque)
        self.check_count = 0

    def allow(self, client_id: str) -> bool:
        now = monotonic()
        cutoff = now - self.window_seconds
        self.check_count += 1
        if self.check_count % 256 == 0:
            for stored_client, stored_timestamps in list(self.attempts.items()):
                while stored_timestamps and stored_timestamps[0] <= cutoff:
                    stored_timestamps.popleft()
                if not stored_timestamps:
                    self.attempts.pop(stored_client, None)
        if client_id not in self.attempts and len(self.attempts) >= self.max_clients:
            return False
        timestamps = self.attempts[client_id]
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()
        if len(timestamps) >= self.limit:
            return False
        timestamps.append(now)
        return True


def _client_id(request: Request) -> str:
    cloudflare_client = request.headers.get("cf-connecting-ip", "").strip()
    if cloudflare_client:
        return cloudflare_client[:80]
    forwarded = request.headers.get("x-forwarded-for", "").rsplit(",", 1)[-1].strip()
    if forwarded:
        return forwarded[:80]
    return request.client.host[:80] if request.client else "unknown"


def _private_response(response: HTMLResponse | JSONResponse) -> HTMLResponse | JSONResponse:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


def create_token_usage_router(
    client: RouterTokenClient | None,
    *,
    limiter: UsageRateLimiter | None = None,
) -> APIRouter:
    router = APIRouter()
    lookup_limiter = limiter or UsageRateLimiter()

    @router.get("/token-usage", response_class=HTMLResponse)
    async def token_usage_page(request: Request) -> HTMLResponse:
        response = templates.TemplateResponse(
            request=request,
            name="token_usage.html",
            context={"service_available": client is not None},
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return _private_response(response)

    @router.post("/api/token-usage")
    async def token_usage_lookup(request: Request) -> JSONResponse:
        if not lookup_limiter.allow(_client_id(request)):
            return _private_response(
                JSONResponse(
                    {"ok": False, "error": "Bạn thao tác quá nhanh, vui lòng thử lại sau."},
                    status_code=429,
                )
            )
        if client is None:
            return _private_response(
                JSONResponse(
                    {"ok": False, "error": "Dịch vụ thống kê đang tạm bảo trì."},
                    status_code=503,
                )
            )
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            content_length = 0
        if content_length > 2_048:
            return _private_response(
                JSONResponse(
                    {"ok": False, "error": "Dữ liệu gửi lên quá lớn."},
                    status_code=413,
                )
            )
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        api_key = str(payload.get("api_key") or "").strip() if isinstance(payload, dict) else ""
        if not api_key.startswith("sk-") or not 12 <= len(api_key) <= 256:
            return _private_response(
                JSONResponse(
                    {"ok": False, "error": "API key không đúng định dạng."},
                    status_code=422,
                )
            )
        try:
            usage = await client.usage(api_key)
        except RouterTokenKeyNotFound:
            return _private_response(
                JSONResponse(
                    {"ok": False, "error": "Không tìm thấy key token của shop."},
                    status_code=404,
                )
            )
        except RouterTokenError:
            return _private_response(
                JSONResponse(
                    {"ok": False, "error": "9Router đang bận, vui lòng thử lại sau."},
                    status_code=502,
                )
            )
        return _private_response(JSONResponse({"ok": True, "usage": usage.public_payload()}))

    return router
