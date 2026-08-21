"""Small Binance Pay Merchant API client used only for wallet deposits."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

import httpx


logger = logging.getLogger(__name__)
BINANCE_PAY_ORDER_PATH = "/binancepay/openapi/v2/order"
DEFAULT_USD_TO_VND = 26_500


class BinancePayError(RuntimeError):
    """Raised when Binance Pay cannot create or validate an order."""


@dataclass(frozen=True)
class BinancePayOrder:
    merchant_trade_no: str
    prepay_id: str
    checkout_url: str
    qr_content: str | None = None
    raw: dict[str, object] | None = None


def vnd_to_usdt(amount: int, usd_to_vnd: int = DEFAULT_USD_TO_VND) -> Decimal:
    """Convert VND to an 8-decimal USDT amount for Binance Pay."""
    rate = Decimal(max(1, int(usd_to_vnd)))
    return (Decimal(max(0, int(amount))) / rate).quantize(
        Decimal("0.00000001"), rounding=ROUND_HALF_UP
    )


def usdt_to_vnd(amount: object, usd_to_vnd: int = DEFAULT_USD_TO_VND) -> int:
    """Convert a provider amount back to VND using the configured quote rate."""
    try:
        value = Decimal(str(amount))
    except Exception as exc:
        raise BinancePayError("Invalid Binance Pay amount") from exc
    if value <= 0:
        raise BinancePayError("Invalid Binance Pay amount")
    return int(
        (value * Decimal(max(1, int(usd_to_vnd)))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def format_usdt_deposit(amount: Decimal) -> str:
    """Format a deposit amount without exposing Binance's 8-decimal precision."""
    text = format(amount.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _format_amount(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP), "f")


def binance_pay_signature(
    raw_body: bytes,
    timestamp: str,
    nonce: str,
    secret_key: str,
) -> str:
    payload = timestamp.encode("ascii") + b"\n" + nonce.encode("ascii") + b"\n"
    payload += raw_body + b"\n"
    digest = hmac.new(secret_key.encode("utf-8"), payload, hashlib.sha512).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_binance_pay_signature(
    raw_body: bytes,
    *,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    secret_key: str,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    if not timestamp or not nonce or not signature or not secret_key:
        return False
    try:
        timestamp_ms = int(timestamp)
    except (TypeError, ValueError):
        return False
    current_ms = int(time.time() * 1000) if now is None else int(now * 1000)
    if abs(current_ms - timestamp_ms) > max(1, int(tolerance_seconds)) * 1000:
        return False
    expected = binance_pay_signature(raw_body, timestamp, nonce, secret_key)
    return hmac.compare_digest(expected, signature.strip())


class BinancePayClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        secret_key: str,
        timeout_seconds: float = 15,
        usd_to_vnd: int = DEFAULT_USD_TO_VND,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.secret_key = secret_key
        self.timeout_seconds = timeout_seconds
        self.usd_to_vnd = max(1, int(usd_to_vnd))

    async def create_order(
        self,
        *,
        merchant_trade_no: str,
        amount_vnd: int | None = None,
        amount_usd_tenths: int | None = None,
        goods_name: str = "Nạp tiền ví PHP Tool Shop",
    ) -> BinancePayOrder:
        if amount_usd_tenths is not None:
            amount_usdt = (
                Decimal(max(0, int(amount_usd_tenths))) / Decimal("10")
            ).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
        elif amount_vnd is not None:
            # Kept for compatibility with old callers; new Binance deposits
            # pass an explicit USD amount and never convert into the VND wallet.
            amount_usdt = vnd_to_usdt(amount_vnd, self.usd_to_vnd)
        else:
            raise BinancePayError("Binance Pay amount is missing")
        if amount_usdt <= 0:
            raise BinancePayError("Deposit amount is too small for Binance Pay")
        body = {
            "env": {"terminalType": "WEB"},
            "merchantTradeNo": merchant_trade_no,
            "orderAmount": {"currency": "USDT", "total": _format_amount(amount_usdt)},
            "goods": {
                "goodsType": "02",
                "goodsCategory": "Z000",
                "referenceGoodsId": "wallet-deposit",
                "goodsName": goods_name[:256],
            },
            "description": f"Nạp ví {merchant_trade_no}"[:256],
        }
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response_data = await self._post(BINANCE_PAY_ORDER_PATH, payload)
        data = response_data.get("data")
        if not isinstance(data, dict):
            raise BinancePayError("Binance Pay returned no order data")
        checkout_url = str(
            data.get("checkoutUrl") or data.get("universalUrl") or data.get("qrContent") or ""
        ).strip()
        prepay_id = str(data.get("prepayId") or "").strip()
        if not checkout_url or not prepay_id:
            raise BinancePayError("Binance Pay returned an incomplete checkout response")
        return BinancePayOrder(
            merchant_trade_no=merchant_trade_no,
            prepay_id=prepay_id,
            checkout_url=checkout_url,
            qr_content=(str(data.get("qrContent")) if data.get("qrContent") else None),
            raw=response_data,
        )

    async def _post(self, path: str, body: bytes) -> dict[str, object]:
        timestamp = str(int(time.time() * 1000))
        nonce = secrets.token_hex(16)
        headers = {
            "Content-Type": "application/json",
            "BinancePay-Timestamp": timestamp,
            "BinancePay-Nonce": nonce,
            "BinancePay-Certificate-SN": self.api_key,
            "BinancePay-Signature": binance_pay_signature(
                body, timestamp, nonce, self.secret_key
            ),
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.base_url + path, content=body, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("Binance Pay request failed: %s", type(exc).__name__)
            raise BinancePayError("Binance Pay is temporarily unavailable") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise BinancePayError("Binance Pay returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise BinancePayError("Binance Pay returned an invalid response")
        if response.status_code >= 400 or str(payload.get("status") or "") != "SUCCESS":
            code = str(payload.get("code") or "unknown")
            message = str(payload.get("errorMessage") or payload.get("message") or "request failed")
            logger.warning(
                "Binance Pay API rejected request: http_status=%s code=%s message=%s",
                response.status_code,
                code[:80],
                message[:160],
            )
            raise BinancePayError(f"Binance Pay request failed ({code}): {message[:160]}")
        return payload


def create_binance_pay_client(settings) -> BinancePayClient | None:
    if not settings.binance_pay_enabled:
        return None
    return BinancePayClient(
        base_url=settings.binance_pay_base_url,
        api_key=settings.binance_pay_api_key.get_secret_value(),
        secret_key=settings.binance_pay_secret_key.get_secret_value(),
        timeout_seconds=settings.binance_pay_timeout_seconds,
        usd_to_vnd=settings.binance_pay_usd_to_vnd,
    )
