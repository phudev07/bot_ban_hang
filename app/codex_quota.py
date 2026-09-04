"""Read-only Codex API quota lookup for the public customer page.

The upstream profile endpoint is intentionally kept in this backend module so
customers never call the gateway directly and gateway credentials are never
exposed to browser code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx


CODEX_QUOTA_PROFILE_PATH = "/api/cockpit-tools/token-profile"
CODEX_QUOTA_TIMEOUT_SECONDS = 15.0
CODEX_KEY_PREFIX = "sk-cdx-"


class CodexQuotaError(Exception):
    """Safe, customer-facing quota lookup failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CodexQuota:
    total: int | None
    used: int | None
    remaining: int | None
    unlimited: bool
    percentage: int | None
    expires_at: int | str | None
    display: str


def validate_codex_key(value: str) -> str:
    key = value.strip()
    if not key.startswith(CODEX_KEY_PREFIX) or len(key) > 512:
        raise CodexQuotaError("INVALID_KEY", "Key không hợp lệ. Key phải bắt đầu bằng sk-cdx-.")
    if any(char.isspace() or ord(char) < 33 or ord(char) > 126 for char in key):
        raise CodexQuotaError("INVALID_KEY", "Key không hợp lệ. Vui lòng kiểm tra lại key.")
    if len(key) <= len(CODEX_KEY_PREFIX):
        raise CodexQuotaError("INVALID_KEY", "Key không hợp lệ. Vui lòng kiểm tra lại key.")
    return key


def quota_profile_url(base_url: str) -> str:
    """Build the same absolute profile path used by Cockpit Tools."""

    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CodexQuotaError("CONFIGURATION_ERROR", "Dịch vụ kiểm tra hạn mức chưa sẵn sàng.")
    if parsed.username or parsed.password:
        raise CodexQuotaError("CONFIGURATION_ERROR", "Dịch vụ kiểm tra hạn mức chưa sẵn sàng.")
    return urlunsplit((parsed.scheme, parsed.netloc, CODEX_QUOTA_PROFILE_PATH, "", ""))


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number)


def _first_number(objects: tuple[Mapping[str, Any] | None, ...], key: str) -> int | None:
    for obj in objects:
        if obj is None:
            continue
        value = _number(obj.get(key))
        if value is not None:
            return value
    return None


def format_token_count(value: int | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000_000 and value % 1_000_000_000 == 0:
        return f"{value // 1_000_000_000}B"
    if value >= 1_000_000 and value % 1_000_000 == 0:
        return f"{value // 1_000_000}M"
    if value >= 1_000 and value % 1_000 == 0:
        return f"{value // 1_000}K"
    return f"{value:,}"


def parse_codex_quota(payload: Any) -> CodexQuota:
    root = _mapping(payload)
    if root is None:
        raise CodexQuotaError("INVALID_RESPONSE", "Không đọc được hạn mức của key.")
    if root.get("success") is False:
        raise CodexQuotaError("INVALID_KEY", "Key không hợp lệ hoặc đã hết hiệu lực.")

    data = _mapping(root.get("data")) or root
    profile = _mapping(data.get("profile"))
    usage = _mapping(data.get("usage")) or _mapping(profile and profile.get("usage"))
    usage = usage or _mapping(root.get("usage"))
    if usage is None:
        raise CodexQuotaError("INVALID_RESPONSE", "Dữ liệu hạn mức không đầy đủ.")

    sources = (usage, data, profile, root)
    total = _first_number(sources, "total_granted")
    used = _first_number(sources, "total_used")
    remaining = _first_number(sources, "total_available")
    unlimited = any(bool(obj.get("unlimited_quota")) for obj in sources if obj is not None)

    if remaining is None and total is not None and used is not None:
        remaining = max(0, total - used)
    if total is None and not unlimited:
        raise CodexQuotaError("INVALID_RESPONSE", "Dữ liệu hạn mức không đầy đủ.")
    if total is not None and remaining is not None:
        remaining = min(max(remaining, 0), total)
    percentage = 100 if unlimited else (
        round((remaining / total) * 100) if total and remaining is not None else None
    )
    if percentage is not None:
        percentage = min(100, max(0, percentage))

    expires_at: int | str | None = None
    for obj in sources:
        if obj is None:
            continue
        candidate = obj.get("expires_at")
        if isinstance(candidate, (int, str)) and str(candidate).strip():
            expires_at = candidate
            break
    summary = None
    for obj in sources:
        if obj is None:
            continue
        candidate = obj.get("summary_display")
        if isinstance(candidate, str) and candidate.strip():
            summary = candidate.strip()
            break
    display = "Không giới hạn" if unlimited else (
        summary or f"{format_token_count(remaining)} / {format_token_count(total)}"
    )
    return CodexQuota(total, used, remaining, unlimited, percentage, expires_at, display)


async def fetch_codex_quota(
    api_key: str,
    *,
    base_url: str,
    client: httpx.AsyncClient | None = None,
) -> CodexQuota:
    key = validate_codex_key(api_key)
    url = quota_profile_url(base_url)
    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=httpx.Timeout(CODEX_QUOTA_TIMEOUT_SECONDS),
        follow_redirects=False,
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )
    try:
        try:
            response = await http_client.get(
                url,
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise CodexQuotaError("UPSTREAM_TIMEOUT", "Không thể cập nhật hạn mức lúc này. Vui lòng thử lại sau.") from exc
        except httpx.HTTPError as exc:
            raise CodexQuotaError("UPSTREAM_UNAVAILABLE", "Dịch vụ kiểm tra hạn mức tạm thời không khả dụng.") from exc

        if response.status_code in {401, 403}:
            raise CodexQuotaError("INVALID_KEY", "Key không hợp lệ hoặc đã hết hiệu lực.")
        if response.status_code == 429:
            raise CodexQuotaError("UPSTREAM_RATE_LIMITED", "Bạn thao tác quá nhanh. Vui lòng thử lại sau.")
        if not 200 <= response.status_code < 300:
            raise CodexQuotaError("UPSTREAM_UNAVAILABLE", "Dịch vụ kiểm tra hạn mức tạm thời không khả dụng.")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CodexQuotaError("INVALID_RESPONSE", "Dịch vụ trả về dữ liệu hạn mức không hợp lệ.") from exc
        return parse_codex_quota(payload)
    finally:
        if owns_client:
            await http_client.aclose()
