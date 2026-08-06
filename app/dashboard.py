import asyncio
import hmac
import logging
import re
import secrets
import time
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, case, cast, delete, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.canboso_suppliers import CanbosoClient
from app.config import Settings
from app.haji_suppliers import HajiClient
from app.inventory_dedup import filter_duplicate_inventory
from app.lehai_suppliers import LeHaiPremiumClient
from app.models import (
    BalanceAdjustment,
    BroadcastDelivery,
    BroadcastLog,
    ApiClient,
    ApiRequestAudit,
    Category,
    Deposit,
    DiscountCode,
    FlashSaleCampaign,
    InventoryDuplicateAlert,
    InventoryItem,
    Order,
    PaymentTransaction,
    Product,
    ProductAlertDelivery,
    ProductPriceAlert,
    ProductStockAlert,
    QuantityDiscount,
    ReferralReward,
    SmsRental,
    SupplierBalanceState,
    SupplierBalanceTransaction,
    SupplierPurchaseAttempt,
    SupplierRecoveryRequest,
    User,
    WalletTransaction,
)
from app.nce_suppliers import NceClient
from app.partner_services import normalize_allowed_ips
from app.rentsim import RentSimClient
from app.services import (
    approve_wallet_deposit,
    buy_supplier_product,
    cancel_wallet_deposit,
    preserve_supplier_purchase_parts,
    refresh_product_from_supplier,
    supplier_balance_clients_for_product,
    supplier_client_for_product,
)
from app.price_alerts import release_price_lock_if_inventory_empty
from app.sms_rentals import sms_availability
from app.stock_alerts import queue_inventory_stock_alert, stock_alert_mode
from app.supplier_audit import PROVIDER, reconcile_supplier_balance, record_supplier_purchase
from app.suppliers import (
    EXTERNAL_FULFILLMENT_SOURCES,
    SELLABLE_FULFILLMENT_SOURCES,
    SumistoreClient,
    SupplierError,
    SupplierRouteFetch,
    configured_supplier_providers,
    enabled_supplier_providers,
    fetch_product_supplier_routes,
    is_multi_supplier_product,
    plan_supplier_routes,
    product_supplier_api_enabled,
    supplier_balance_guard,
    supplier_route_sort_key,
)
from app.utils import SecretCipher, format_usd_from_vnd, format_vnd, parse_vnd
from app.wallet_ledger import apply_wallet_change
from app.dashboard_security import new_csrf_token, verify_dashboard_password


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["vnd"] = format_vnd
templates.env.filters["usd_from_vnd"] = format_usd_from_vnd


LOCAL_TIMEZONE = ZoneInfo("Asia/Bangkok")
ADMIN_PAGE_SIZE = 100
MAX_FLASH_SALE_IMAGE_BYTES = 8 * 1024 * 1024
MAX_INVENTORY_WITHDRAWAL_QUANTITY = 1000
logger = logging.getLogger(__name__)
SUPPLIER_PROVIDER_LABELS = {
    "sumistore": "Sumi",
    "lehai": "Lê Hải",
    "canboso": "Canboso",
    "nce": "API Codex & Claude",
    "haji": "Haji",
}

WALLET_KIND_LABELS = {
    "opening_balance": "Số dư đầu kỳ",
    "deposit": "Nạp tiền",
    "direct_purchase_fallback": "Tiền mua chuyển vào ví",
    "product_purchase": "Mua hàng",
    "sms_rental": "Thuê số SMS",
    "sms_refund": "Hoàn tiền thuê số",
    "referral_commission": "Hoa hồng giới thiệu",
    "admin_adjustment": "Admin điều chỉnh",
}
WALLET_REFERENCE_LABELS = {
    "system": "Hệ thống",
    "order": "Đơn hàng",
    "deposit": "Mã nạp",
    "sms_rental": "Đơn thuê số",
    "referral": "Đơn giới thiệu",
    "balance_adjustment": "Điều chỉnh Admin",
}


@dataclass(frozen=True)
class AdminPager:
    page: int
    total_pages: int
    total_items: int
    start_item: int
    end_item: int
    previous_url: str | None
    next_url: str | None

    @property
    def offset(self) -> int:
        return (self.page - 1) * ADMIN_PAGE_SIZE


def admin_pager(
    request: Request,
    total_items: int,
    requested_page: int,
    *,
    page_parameter: str = "page",
) -> AdminPager:
    total = max(0, int(total_items))
    total_pages = max(1, (total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    page = min(max(1, int(requested_page)), total_pages)

    def page_url(target_page: int) -> str:
        parameters = dict(request.query_params)
        parameters[page_parameter] = str(target_page)
        return f"{request.url.path}?{urlencode(parameters)}"

    start_item = (page - 1) * ADMIN_PAGE_SIZE + 1 if total else 0
    end_item = min(page * ADMIN_PAGE_SIZE, total)
    return AdminPager(
        page=page,
        total_pages=total_pages,
        total_items=total,
        start_item=start_item,
        end_item=end_item,
        previous_url=page_url(page - 1) if page > 1 else None,
        next_url=page_url(page + 1) if page < total_pages else None,
    )


def local_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TIMEZONE).strftime("%d/%m/%Y %H:%M:%S")


templates.env.filters["localtime"] = local_datetime


def dashboard_periods() -> dict[str, datetime]:
    now = datetime.now(LOCAL_TIMEZONE)
    return {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC),
        "month": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC),
        "year": now.replace(
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).astimezone(UTC),
        "seven_days": (now - timedelta(days=6)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).astimezone(UTC),
        "fourteen_days": (now - timedelta(days=13)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).astimezone(UTC),
        "thirty_days": (now - timedelta(days=29)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        ).astimezone(UTC),
    }


def order_group_key():
    return func.coalesce(Order.batch_code, literal("O") + cast(Order.id, String))


def purchase_order_count():
    return func.count(func.distinct(order_group_key()))


def order_supplier_provider(order: Order) -> str:
    if order.supplier_provider in EXTERNAL_FULFILLMENT_SOURCES:
        return order.supplier_provider
    code = order.supplier_order_code or ""
    if code.startswith("API-TELE-"):
        return "sumistore"
    if code.startswith("LHP-"):
        return "lehai"
    if code.startswith("CBS-"):
        return "canboso"
    if code.startswith("NCE-"):
        return "nce"
    if code.startswith("HAJI-"):
        return "haji"
    return "local"


def order_supplier_provider_expression():
    return case(
        (
            Order.supplier_provider.in_(EXTERNAL_FULFILLMENT_SOURCES),
            Order.supplier_provider,
        ),
        (Order.supplier_order_code.like("API-TELE-%"), "sumistore"),
        (Order.supplier_order_code.like("LHP-%"), "lehai"),
        (Order.supplier_order_code.like("CBS-%"), "canboso"),
        (Order.supplier_order_code.like("NCE-%"), "nce"),
        (Order.supplier_order_code.like("HAJI-%"), "haji"),
        else_="local",
    )


def order_supplier_label(providers: set[str]) -> str:
    ordered = [
        provider
        for provider in ("sumistore", "canboso", "lehai", "nce", "haji", "local")
        if provider in providers
    ]
    return " + ".join(
        SUPPLIER_PROVIDER_LABELS.get(provider, "Kho bot")
        for provider in ordered
    )


def group_order_rows(rows, limit: int | None = None) -> list[dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    for order, product, user in rows:
        key = order.shop_order_code
        group = groups.get(key)
        if group is None:
            group = {
                "primary_order_id": order.id,
                "shop_order_code": key,
                "supplier_order_code": order.supplier_order_code,
                "supplier_order_codes": (
                    [order.supplier_order_code] if order.supplier_order_code else []
                ),
                "supplier_providers": set(),
                "sales_channel": order.sales_channel,
                "quantity": 0,
                "amount": 0,
                "cost_amount": 0,
                "discount_amount": 0,
                "discount_code": order.discount_code,
                "status": order.status,
                "created_at": order.created_at,
                "delivered_at": order.delivered_at,
                "product": product,
                "product_name_vi": order.product_name_vi or product.name_vi,
                "product_name_en": order.product_name_en or product.name_en,
                "user": user,
                "item_ids": [],
            }
            groups[key] = group
        group["primary_order_id"] = min(int(group["primary_order_id"]), order.id)
        group["quantity"] = int(group["quantity"]) + 1
        group["amount"] = int(group["amount"]) + int(order.amount)
        group["cost_amount"] = int(group["cost_amount"]) + int(order.cost_amount)
        group["discount_amount"] = int(group["discount_amount"]) + int(
            order.discount_amount
        )
        group["item_ids"].append(order.id)
        group["supplier_providers"].add(order_supplier_provider(order))
        if (
            order.supplier_order_code
            and order.supplier_order_code not in group["supplier_order_codes"]
        ):
            group["supplier_order_codes"].append(order.supplier_order_code)
            group["supplier_order_code"] = " · ".join(
                group["supplier_order_codes"]
            )
        if not group["supplier_order_code"] and order.supplier_order_code:
            group["supplier_order_code"] = order.supplier_order_code
        if order.status != "completed":
            group["status"] = order.status
        if order.created_at < group["created_at"]:
            group["created_at"] = order.created_at
        delivered_at = group["delivered_at"]
        if order.delivered_at is not None and (
            delivered_at is None or order.delivered_at > delivered_at
        ):
            group["delivered_at"] = order.delivered_at
    grouped = list(groups.values())
    for group in grouped:
        providers = group["supplier_providers"]
        group["supplier_source_label"] = order_supplier_label(providers)
        group["supplier_source_external"] = any(
            provider in EXTERNAL_FULFILLMENT_SOURCES for provider in providers
        )
    return grouped[:limit] if limit is not None else grouped


def parse_local_datetime(value: str) -> datetime | None:
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
    return parsed.astimezone(UTC)


async def financial_summary(
    session: AsyncSession,
    start_at: datetime | None = None,
) -> dict[str, int | float]:
    statement = select(
        purchase_order_count(),
        func.count(Order.id),
        func.coalesce(func.sum(Order.amount), 0),
        func.coalesce(func.sum(Order.cost_amount), 0),
        func.coalesce(func.sum(Order.discount_amount), 0),
    ).join(Product, Product.id == Order.product_id).where(
        Order.status == "completed",
        Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
        Product.product_type == "account",
    )
    if start_at is not None:
        statement = statement.where(Order.created_at >= start_at)
    order_count, account_count, revenue, cost, discount = (
        await session.execute(statement)
    ).one()
    sms_statement = select(
        func.count(SmsRental.id),
        func.coalesce(func.sum(SmsRental.sale_amount), 0),
        func.coalesce(func.sum(SmsRental.cost_amount), 0),
    ).where(SmsRental.status == "success")
    if start_at is not None:
        sms_statement = sms_statement.where(SmsRental.completed_at >= start_at)
    sms_count, sms_revenue, sms_cost = (await session.execute(sms_statement)).one()
    order_count = int(order_count) + int(sms_count)
    revenue = int(revenue) + int(sms_revenue)
    cost = int(cost) + int(sms_cost)
    reward_statement = select(
        func.coalesce(func.sum(ReferralReward.commission_amount), 0)
    )
    if start_at is not None:
        reward_statement = reward_statement.where(ReferralReward.created_at >= start_at)
    referral = int(await session.scalar(reward_statement) or 0)
    gross_profit = revenue - cost
    profit = gross_profit - referral
    return {
        "orders": order_count,
        "accounts": int(account_count),
        "revenue": revenue,
        "cost": cost,
        "gross_profit": gross_profit,
        "referral": referral,
        "profit": profit,
        "discount": int(discount),
        "margin": round(profit / revenue * 100, 1) if revenue else 0,
    }


async def financial_summaries(
    session: AsyncSession,
    periods: dict[str, datetime],
) -> dict[str, dict[str, int | float]]:
    """Read all dashboard periods in one aggregate query."""
    def count_since(start_at: datetime):
        return purchase_order_count().filter(Order.created_at >= start_at)

    def account_count_since(start_at: datetime):
        return func.count(Order.id).filter(Order.created_at >= start_at)

    def sum_since(column, start_at: datetime):
        return func.coalesce(func.sum(column).filter(Order.created_at >= start_at), 0)

    statement = select(
        count_since(periods["today"]).label("today_orders"),
        account_count_since(periods["today"]).label("today_accounts"),
        sum_since(Order.amount, periods["today"]).label("today_revenue"),
        sum_since(Order.cost_amount, periods["today"]).label("today_cost"),
        sum_since(Order.discount_amount, periods["today"]).label("today_discount"),
        count_since(periods["month"]).label("month_orders"),
        account_count_since(periods["month"]).label("month_accounts"),
        sum_since(Order.amount, periods["month"]).label("month_revenue"),
        sum_since(Order.cost_amount, periods["month"]).label("month_cost"),
        sum_since(Order.discount_amount, periods["month"]).label("month_discount"),
        count_since(periods["year"]).label("year_orders"),
        account_count_since(periods["year"]).label("year_accounts"),
        sum_since(Order.amount, periods["year"]).label("year_revenue"),
        sum_since(Order.cost_amount, periods["year"]).label("year_cost"),
        sum_since(Order.discount_amount, periods["year"]).label("year_discount"),
        purchase_order_count().label("all_orders"),
        func.count(Order.id).label("all_accounts"),
        func.coalesce(func.sum(Order.amount), 0).label("all_revenue"),
        func.coalesce(func.sum(Order.cost_amount), 0).label("all_cost"),
        func.coalesce(func.sum(Order.discount_amount), 0).label("all_discount"),
    ).join(Product, Product.id == Order.product_id).where(
        Order.status == "completed",
        Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
        Product.product_type == "account",
    )
    values = (await session.execute(statement)).one()
    sms_values = (
        await session.execute(
            select(
                func.count(SmsRental.id)
                .filter(
                    SmsRental.status == "success",
                    SmsRental.completed_at >= periods["today"],
                )
                .label("today_orders"),
                func.coalesce(
                    func.sum(SmsRental.sale_amount).filter(
                        SmsRental.status == "success",
                        SmsRental.completed_at >= periods["today"],
                    ),
                    0,
                ).label("today_revenue"),
                func.coalesce(
                    func.sum(SmsRental.cost_amount).filter(
                        SmsRental.status == "success",
                        SmsRental.completed_at >= periods["today"],
                    ),
                    0,
                ).label("today_cost"),
                func.count(SmsRental.id)
                .filter(
                    SmsRental.status == "success",
                    SmsRental.completed_at >= periods["month"],
                )
                .label("month_orders"),
                func.coalesce(
                    func.sum(SmsRental.sale_amount).filter(
                        SmsRental.status == "success",
                        SmsRental.completed_at >= periods["month"],
                    ),
                    0,
                ).label("month_revenue"),
                func.coalesce(
                    func.sum(SmsRental.cost_amount).filter(
                        SmsRental.status == "success",
                        SmsRental.completed_at >= periods["month"],
                    ),
                    0,
                ).label("month_cost"),
                func.count(SmsRental.id)
                .filter(
                    SmsRental.status == "success",
                    SmsRental.completed_at >= periods["year"],
                )
                .label("year_orders"),
                func.coalesce(
                    func.sum(SmsRental.sale_amount).filter(
                        SmsRental.status == "success",
                        SmsRental.completed_at >= periods["year"],
                    ),
                    0,
                ).label("year_revenue"),
                func.coalesce(
                    func.sum(SmsRental.cost_amount).filter(
                        SmsRental.status == "success",
                        SmsRental.completed_at >= periods["year"],
                    ),
                    0,
                ).label("year_cost"),
                func.count(SmsRental.id)
                .filter(SmsRental.status == "success")
                .label("all_orders"),
                func.coalesce(
                    func.sum(SmsRental.sale_amount).filter(SmsRental.status == "success"),
                    0,
                ).label("all_revenue"),
                func.coalesce(
                    func.sum(SmsRental.cost_amount).filter(SmsRental.status == "success"),
                    0,
                ).label("all_cost"),
            )
        )
    ).one()
    reward_values = (
        await session.execute(
            select(
                func.coalesce(
                    func.sum(ReferralReward.commission_amount).filter(
                        ReferralReward.created_at >= periods["today"]
                    ),
                    0,
                ).label("today_referral"),
                func.coalesce(
                    func.sum(ReferralReward.commission_amount).filter(
                        ReferralReward.created_at >= periods["month"]
                    ),
                    0,
                ).label("month_referral"),
                func.coalesce(
                    func.sum(ReferralReward.commission_amount).filter(
                        ReferralReward.created_at >= periods["year"]
                    ),
                    0,
                ).label("year_referral"),
                func.coalesce(func.sum(ReferralReward.commission_amount), 0).label(
                    "all_referral"
                ),
            )
        )
    ).one()
    fields = values._mapping
    sms_fields = sms_values._mapping
    reward_fields = reward_values._mapping
    result: dict[str, dict[str, int | float]] = {}
    for key in ("today", "month", "year", "all"):
        revenue = int(fields[f"{key}_revenue"]) + int(sms_fields[f"{key}_revenue"])
        cost = int(fields[f"{key}_cost"]) + int(sms_fields[f"{key}_cost"])
        referral = int(reward_fields[f"{key}_referral"])
        gross_profit = revenue - cost
        profit = gross_profit - referral
        result[key] = {
            "orders": int(fields[f"{key}_orders"]) + int(sms_fields[f"{key}_orders"]),
            "accounts": int(fields[f"{key}_accounts"]),
            "revenue": revenue,
            "cost": cost,
            "gross_profit": gross_profit,
            "referral": referral,
            "profit": profit,
            "discount": int(fields[f"{key}_discount"]),
            "margin": round(profit / revenue * 100, 1) if revenue else 0,
        }
    return result


def redirect_to_login() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def is_admin(request: Request) -> bool:
    return bool(request.session.get("dashboard_admin"))


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = new_csrf_token()
        request.session["csrf_token"] = token
    return str(token)


def valid_csrf(request: Request, submitted: str) -> bool:
    expected = str(request.session.get("csrf_token") or "")
    return bool(submitted and expected and hmac.compare_digest(submitted, expected))


def wants_dashboard_json(request: Request) -> bool:
    return request.headers.get("x-requested-with", "").lower() == "xmlhttprequest"


def flash(request: Request, message: str, level: str = "success") -> None:
    request.session["flash"] = {"message": message, "level": level}


def page_context(request: Request, title: str, section: str, **values: object) -> dict[str, object]:
    return {
        "request": request,
        "title": title,
        "section": section,
        "csrf_token": csrf_token(request),
        "admin_username": request.session.get("dashboard_admin", ""),
        "flash": request.session.pop("flash", None),
        **values,
    }


def split_inventory_items(raw: str) -> list[str]:
    normalized = raw.replace("\r\n", "\n").strip()
    if not normalized:
        return []
    if "\n---\n" in normalized:
        return [item.strip() for item in normalized.split("\n---\n") if item.strip()]
    return [line.strip() for line in normalized.splitlines() if line.strip()]


def normalize_product_type(value: str) -> str | None:
    normalized = value.strip().lower()
    return normalized if normalized == "account" else None


def normalize_fulfillment_source(value: str) -> str | None:
    normalized = value.strip().lower()
    return normalized if normalized in SELLABLE_FULFILLMENT_SOURCES else None


def default_flash_sale_message(
    product_name: str,
    original_price: int,
    sale_price: int,
    quantity: int,
) -> str:
    return (
        "⚡ <b>FLASH SALE GIỚI HẠN</b>\n\n"
        f"• Sản phẩm: <b>{escape(product_name)}</b>\n"
        f"• Giá cũ: <s>{format_vnd(original_price)}</s>\n"
        f"• Giá Flash Sale: <b>{format_vnd(sale_price)}</b>\n"
        f"• Số lượng ưu đãi: <b>{quantity}</b>\n\n"
        "Nhanh tay mua trước khi hết suất."
    )


def balance_adjustment_notification(
    *,
    amount: int,
    balance: int,
    reason: str,
    language: str,
) -> str:
    signed_amount = f"{'+' if amount > 0 else '-'}{format_vnd(abs(amount))}"
    if language == "en":
        return (
            "💰 <b>Your wallet balance was adjusted</b>\n\n"
            f"• Change: <b>{signed_amount}</b>\n"
            f"• Reason: <b>{escape(reason)}</b>\n"
            f"• Current balance: <b>{format_vnd(balance)}</b>\n\n"
            "This adjustment was made by the shop administrator."
        )
    return (
        "💰 <b>Số dư ví đã được điều chỉnh</b>\n\n"
        f"• Thay đổi: <b>{signed_amount}</b>\n"
        f"• Lý do: <b>{escape(reason)}</b>\n"
        f"• Số dư hiện tại: <b>{format_vnd(balance)}</b>\n\n"
        "Điều chỉnh này được thực hiện bởi quản trị viên của shop."
    )


def create_dashboard_router(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cipher: SecretCipher,
    supplier_client: SumistoreClient | None = None,
    lehai_client: LeHaiPremiumClient | None = None,
    rentsim_client: RentSimClient | None = None,
    bot: Bot | None = None,
    *,
    canboso_client: CanbosoClient | None = None,
    nce_client: NceClient | None = None,
    haji_client: HajiClient | None = None,
) -> APIRouter:
    router = APIRouter()
    route_cache: dict[tuple[str, ...], tuple[float, SupplierRouteFetch]] = {}
    route_cache_lock = asyncio.Lock()

    async def multi_source_route_fetch(
        product: Product,
    ) -> SupplierRouteFetch | None:
        if (
            supplier_client is None
            and lehai_client is None
            and canboso_client is None
            and nce_client is None
            and haji_client is None
        ):
            return None
        if not product.supplier_product_id:
            return None
        enabled_providers = enabled_supplier_providers(product)
        cache_key = (
            product.fulfillment_source,
            product.supplier_product_id,
            *sorted(enabled_providers),
        )
        now = time.monotonic()
        cache_seconds = max(5, settings.supplier_ui_cache_seconds)
        cached = route_cache.get(cache_key)
        if cached is not None and now - cached[0] < cache_seconds:
            return cached[1]
        async with route_cache_lock:
            now = time.monotonic()
            cached = route_cache.get(cache_key)
            if cached is not None and now - cached[0] < cache_seconds:
                return cached[1]
            fetched = await fetch_product_supplier_routes(
                product.fulfillment_source,
                product.supplier_product_id,
                supplier_client,
                lehai_client,
                canboso_client,
                nce_client,
                haji_client,
                enabled_providers=enabled_providers,
            )
            route_cache[cache_key] = (now, fetched)
            return fetched

    def supplier_source_rows(product: Product) -> list[dict[str, object]]:
        return [
            {
                "provider": provider,
                "label": SUPPLIER_PROVIDER_LABELS.get(provider, provider),
                "enabled": product_supplier_api_enabled(product, provider),
            }
            for provider in configured_supplier_providers(
                product.fulfillment_source,
                product.supplier_product_id,
            )
        ]

    async def upload_flash_sale_image(image: UploadFile | None) -> str | None:
        if image is None or not image.filename:
            return None
        if not (image.content_type or "").lower().startswith("image/"):
            raise ValueError("Tệp đính kèm phải là ảnh.")
        content = await image.read(MAX_FLASH_SALE_IMAGE_BYTES + 1)
        if not content:
            raise ValueError("Ảnh đính kèm đang trống.")
        if len(content) > MAX_FLASH_SALE_IMAGE_BYTES:
            raise ValueError("Ảnh Flash Sale không được lớn hơn 8 MB.")
        if bot is None or not settings.admin_ids:
            raise ValueError("Bot chưa sẵn sàng để lưu ảnh Flash Sale.")
        admin_chat_id = settings.admin_ids[0]
        try:
            preview = await bot.send_photo(
                admin_chat_id,
                BufferedInputFile(content, filename=image.filename),
            )
        except TelegramBadRequest as exc:
            raise ValueError("Telegram không nhận ảnh này. Hãy thử ảnh JPG/PNG khác.") from exc
        except Exception as exc:
            logger.exception("Could not upload Flash Sale photo to Telegram")
            raise ValueError("Không thể tải ảnh lên Telegram lúc này.") from exc
        try:
            if not preview.photo:
                raise ValueError("Telegram không trả về mã ảnh hợp lệ.")
            return preview.photo[-1].file_id
        finally:
            with suppress(Exception):
                await bot.delete_message(admin_chat_id, preview.message_id)

    @router.get("/admin/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        if is_admin(request):
            return RedirectResponse("/admin", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "title": "Đăng nhập quản trị",
                "error": None,
            },
        )

    @router.post("/admin/login", response_class=HTMLResponse)
    async def login(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ) -> Response:
        password_ok = verify_dashboard_password(
            password,
            settings.dashboard_password_hash.get_secret_value(),
        )
        if username != settings.dashboard_username or not password_ok:
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "request": request,
                    "title": "Đăng nhập quản trị",
                    "error": "Tên đăng nhập hoặc mật khẩu không đúng.",
                },
                status_code=401,
            )
        request.session.clear()
        request.session["dashboard_admin"] = settings.dashboard_username
        request.session["csrf_token"] = new_csrf_token()
        return RedirectResponse("/admin", status_code=303)

    @router.post("/admin/logout")
    async def logout(request: Request, csrf: str = Form(...)) -> RedirectResponse:
        if valid_csrf(request, csrf):
            request.session.clear()
        return RedirectResponse("/admin/login", status_code=303)

    @router.get("/admin", response_class=HTMLResponse)
    async def dashboard_home(request: Request) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        periods = dashboard_periods()
        async with session_factory() as session:
            financials = await financial_summaries(session, periods)
            users = int(await session.scalar(select(func.count(User.telegram_id))) or 0)
            users_today = int(
                await session.scalar(
                    select(func.count(User.telegram_id)).where(
                        User.created_at >= periods["today"]
                    )
                )
                or 0
            )
            users_month = int(
                await session.scalar(
                    select(func.count(User.telegram_id)).where(
                        User.created_at >= periods["month"]
                    )
                )
                or 0
            )
            users_year = int(
                await session.scalar(
                    select(func.count(User.telegram_id)).where(User.created_at >= periods["year"])
                )
                or 0
            )
            active_recipients = int(
                await session.scalar(
                    select(func.count(User.telegram_id)).where(User.has_started.is_(True))
                )
                or 0
            )
            blocked_users = int(
                await session.scalar(
                    select(func.count(User.telegram_id)).where(User.is_blocked.is_(True))
                )
                or 0
            )
            orders = int(financials["all"]["orders"])
            orders_today = int(financials["today"]["orders"])
            orders_month = int(financials["month"]["orders"])
            orders_year = int(financials["year"]["orders"])
            accounts = int(financials["all"]["accounts"])
            accounts_today = int(financials["today"]["accounts"])
            accounts_month = int(financials["month"]["accounts"])
            accounts_year = int(financials["year"]["accounts"])
            revenue = int(financials["all"]["revenue"])
            revenue_today = int(financials["today"]["revenue"])
            revenue_month = int(financials["month"]["revenue"])
            revenue_year = int(financials["year"]["revenue"])
            stock = int(
                await session.scalar(
                    select(func.count(InventoryItem.id))
                    .join(Product, Product.id == InventoryItem.product_id)
                    .where(
                        InventoryItem.status == "available",
                        Product.fulfillment_source == "local",
                        Product.force_out_of_stock.is_(False),
                        Product.product_type == "account",
                    )
                )
                or 0
            )
            stock += int(
                await session.scalar(
                    select(func.coalesce(func.sum(Product.external_stock), 0)).where(
                        Product.fulfillment_source.in_(EXTERNAL_FULFILLMENT_SOURCES),
                        Product.force_out_of_stock.is_(False),
                    )
                )
                or 0
            )
            pending = int(
                await session.scalar(
                    select(func.count(Deposit.id)).where(Deposit.status == "pending")
                )
                or 0
            )
            pending_amount = int(
                await session.scalar(
                    select(func.coalesce(func.sum(Deposit.requested_amount), 0)).where(
                        Deposit.status == "pending"
                    )
                )
                or 0
            )
            received_today = int(
                await session.scalar(
                    select(func.coalesce(func.sum(PaymentTransaction.amount), 0)).where(
                        PaymentTransaction.created_at >= periods["today"],
                        PaymentTransaction.credit_status == "credited",
                    )
                )
                or 0
            )
            wallet_total = int(
                await session.scalar(select(func.coalesce(func.sum(User.balance), 0))) or 0
            )
            account_buyers = set(
                await session.scalars(
                    select(Order.user_id)
                    .join(Product, Product.id == Order.product_id)
                    .where(
                        Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                        Product.product_type == "account",
                    )
                    .distinct()
                )
            )
            sms_buyers = set(
                await session.scalars(
                    select(SmsRental.user_id)
                    .where(SmsRental.status == "success")
                    .distinct()
                )
            )
            buying_users = len(account_buyers | sms_buyers)
            rows = await session.execute(
                select(Order, Product, User)
                .join(Product, Product.id == Order.product_id)
                .join(User, User.telegram_id == Order.user_id)
                .where(
                    Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                    Product.product_type == "account",
                )
                .order_by(Order.id.desc())
                .limit(800)
            )
            recent_orders = group_order_rows(rows, limit=8)
            recent_users = list(
                await session.scalars(select(User).order_by(User.created_at.desc()).limit(6))
            )
            sale_alert_count = int(
                await session.scalar(select(func.count(ProductPriceAlert.id))) or 0
            )
            recent_sale_alerts = [
                {"alert": alert, "product": product}
                for alert, product in (
                    await session.execute(
                        select(ProductPriceAlert, Product)
                        .join(Product, Product.id == ProductPriceAlert.product_id)
                        .order_by(ProductPriceAlert.id.desc())
                        .limit(8)
                    )
                ).all()
            ]
            stock_alert_count = int(
                await session.scalar(select(func.count(ProductStockAlert.id))) or 0
            )
            recent_stock_alerts = [
                {"alert": alert, "product": product}
                for alert, product in (
                    await session.execute(
                        select(ProductStockAlert, Product)
                        .join(Product, Product.id == ProductStockAlert.product_id)
                        .order_by(ProductStockAlert.id.desc())
                        .limit(8)
                    )
                ).all()
            ]
            top_product_rows = await session.execute(
                select(
                    Product,
                    purchase_order_count(),
                    func.count(Order.id),
                    func.coalesce(func.sum(Order.amount), 0),
                    func.coalesce(func.sum(Order.cost_amount), 0),
                    func.coalesce(func.sum(Order.discount_amount), 0),
                )
                .join(Order, Order.product_id == Product.id)
                .where(
                    Order.status == "completed",
                    Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                    Product.product_type == "account",
                )
                .group_by(Product.id)
                .order_by(func.sum(Order.amount).desc())
                .limit(5)
            )
            top_products = [
                {
                    "product": product,
                    "orders": int(count),
                    "accounts": int(account_count),
                    "revenue": int(total),
                    "cost": int(cost),
                    "profit": int(total) - int(cost),
                    "discount": int(discount),
                }
                for product, count, account_count, total, cost, discount in top_product_rows
            ]
            sales_rows = await session.execute(
                select(Order.created_at, Order.amount, Order.cost_amount)
                .join(Product, Product.id == Order.product_id)
                .where(
                    Order.created_at >= periods["fourteen_days"],
                    Order.status == "completed",
                    Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                    Product.product_type == "account",
                )
            )
            year_sales_rows = await session.execute(
                select(
                    Order.created_at,
                    Order.amount,
                    Order.cost_amount,
                    Order.discount_amount,
                    order_group_key(),
                )
                .join(Product, Product.id == Order.product_id)
                .where(
                    Order.created_at >= periods["year"],
                    Order.status == "completed",
                    Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                    Product.product_type == "account",
                )
            )
            sms_sales_rows = await session.execute(
                select(
                    SmsRental.completed_at,
                    SmsRental.sale_amount,
                    SmsRental.cost_amount,
                ).where(
                    SmsRental.status == "success",
                    SmsRental.completed_at >= periods["fourteen_days"],
                )
            )
            sms_year_sales_rows = await session.execute(
                select(
                    SmsRental.completed_at,
                    SmsRental.sale_amount,
                    SmsRental.cost_amount,
                    SmsRental.shop_order_code,
                ).where(
                    SmsRental.status == "success",
                    SmsRental.completed_at >= periods["year"],
                )
            )
            trend_reward_rows = await session.execute(
                select(ReferralReward.created_at, ReferralReward.commission_amount).where(
                    ReferralReward.created_at >= periods["fourteen_days"]
                )
            )
            year_reward_rows = await session.execute(
                select(ReferralReward.created_at, ReferralReward.commission_amount).where(
                    ReferralReward.created_at >= periods["year"]
                )
            )

        today_local = datetime.now(LOCAL_TIMEZONE).date()
        sales_by_day = {
            today_local - timedelta(days=offset): {"revenue": 0, "profit": 0}
            for offset in range(13, -1, -1)
        }
        for created_at, amount, cost in sales_rows:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            local_day = created_at.astimezone(LOCAL_TIMEZONE).date()
            if local_day in sales_by_day:
                sales_by_day[local_day]["revenue"] += int(amount)
                sales_by_day[local_day]["profit"] += int(amount) - int(cost)
        for created_at, amount, cost in sms_sales_rows:
            if created_at is None:
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            local_day = created_at.astimezone(LOCAL_TIMEZONE).date()
            if local_day in sales_by_day:
                sales_by_day[local_day]["revenue"] += int(amount)
                sales_by_day[local_day]["profit"] += int(amount) - int(cost)
        for created_at, commission in trend_reward_rows:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            local_day = created_at.astimezone(LOCAL_TIMEZONE).date()
            if local_day in sales_by_day:
                sales_by_day[local_day]["profit"] -= int(commission)
        trend_max = max(
            (value["revenue"] for value in sales_by_day.values()),
            default=0,
        )
        sales_trend = [
            {
                "label": day.strftime("%d/%m"),
                "amount": values["revenue"],
                "profit": values["profit"],
                "height": (
                    max(5, round(values["revenue"] / trend_max * 100)) if trend_max else 5
                ),
                "profit_height": (
                    max(3, round(max(0, values["profit"]) / trend_max * 100))
                    if trend_max
                    else 3
                ),
            }
            for day, values in sales_by_day.items()
        ]
        now_local = datetime.now(LOCAL_TIMEZONE)
        monthly_values = {
            month: {
                "revenue": 0,
                "cost": 0,
                "referral": 0,
                "profit": 0,
                "discount": 0,
                "orders": 0,
                "accounts": 0,
            }
            for month in range(1, now_local.month + 1)
        }
        monthly_order_keys: dict[int, set[str]] = {
            month: set() for month in range(1, now_local.month + 1)
        }
        for created_at, amount, cost, discount, group_key in year_sales_rows:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            month = created_at.astimezone(LOCAL_TIMEZONE).month
            if month in monthly_values:
                monthly_values[month]["revenue"] += int(amount)
                monthly_values[month]["cost"] += int(cost)
                monthly_values[month]["profit"] += int(amount) - int(cost)
                monthly_values[month]["discount"] += int(discount)
                monthly_values[month]["accounts"] += 1
                monthly_order_keys[month].add(str(group_key))
        for created_at, amount, cost, group_key in sms_year_sales_rows:
            if created_at is None:
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            month = created_at.astimezone(LOCAL_TIMEZONE).month
            if month in monthly_values:
                monthly_values[month]["revenue"] += int(amount)
                monthly_values[month]["cost"] += int(cost)
                monthly_values[month]["profit"] += int(amount) - int(cost)
                monthly_order_keys[month].add(str(group_key))
        for created_at, commission in year_reward_rows:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            month = created_at.astimezone(LOCAL_TIMEZONE).month
            if month in monthly_values:
                monthly_values[month]["referral"] += int(commission)
                monthly_values[month]["profit"] -= int(commission)
        for month, keys in monthly_order_keys.items():
            monthly_values[month]["orders"] = len(keys)
        monthly_performance = [
            {"label": f"Tháng {month}", **values}
            for month, values in reversed(monthly_values.items())
        ]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            page_context(
                request,
                "Tổng quan",
                "dashboard",
                stats={
                    "users": users,
                    "users_today": users_today,
                    "users_month": users_month,
                    "users_year": users_year,
                    "active_recipients": active_recipients,
                    "blocked_users": blocked_users,
                    "orders": orders,
                    "orders_today": orders_today,
                    "orders_month": orders_month,
                    "orders_year": orders_year,
                    "accounts": accounts,
                    "accounts_today": accounts_today,
                    "accounts_month": accounts_month,
                    "accounts_year": accounts_year,
                    "revenue": revenue,
                    "revenue_today": revenue_today,
                    "revenue_month": revenue_month,
                    "revenue_year": revenue_year,
                    "stock": stock,
                    "pending": pending,
                    "pending_amount": pending_amount,
                    "received_today": received_today,
                    "wallet_total": wallet_total,
                    "average_order": revenue // orders if orders else 0,
                    "buyer_rate": round(buying_users / users * 100, 1) if users else 0,
                },
                financials=financials,
                recent_orders=recent_orders,
                recent_users=recent_users,
                sale_alert_count=sale_alert_count,
                recent_sale_alerts=recent_sale_alerts,
                stock_alert_count=stock_alert_count,
                recent_stock_alerts=recent_stock_alerts,
                top_products=top_products,
                sales_trend=sales_trend,
                monthly_performance=monthly_performance,
            ),
        )

    @router.get("/admin/categories", response_class=HTMLResponse)
    async def categories_page(request: Request) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        async with session_factory() as session:
            rows = list(
                await session.scalars(
                    select(Category)
                    .where(Category.archived_at.is_(None))
                    .order_by(Category.position, Category.id)
                )
            )
        return templates.TemplateResponse(
            request,
            "categories.html",
            page_context(request, "Gian hàng", "categories", categories=rows),
        )

    @router.post("/admin/categories")
    async def create_category(
        request: Request,
        csrf: str = Form(...),
        name_vi: str = Form(...),
        name_en: str = Form(""),
        position: int = Form(0),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            flash(request, "Phiên biểu mẫu không hợp lệ.", "error")
            return RedirectResponse("/admin/categories", status_code=303)
        normalized_name = name_vi.strip()
        if not normalized_name:
            flash(request, "Tên gian hàng không được để trống.", "error")
            return RedirectResponse("/admin/categories", status_code=303)
        async with session_factory() as session:
            session.add(
                Category(
                    name_vi=normalized_name,
                    name_en=name_en.strip() or normalized_name,
                    position=position,
                )
            )
            await session.commit()
        flash(request, "Đã tạo gian hàng mới.")
        return RedirectResponse("/admin/categories", status_code=303)

    @router.post("/admin/categories/{category_id}")
    async def update_category(
        category_id: int,
        request: Request,
        csrf: str = Form(...),
        name_vi: str = Form(...),
        name_en: str = Form(""),
        position: int = Form(0),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/categories", status_code=303)
        normalized_name = name_vi.strip()
        async with session_factory() as session:
            category = await session.get(Category, category_id)
            if category is None or not normalized_name:
                flash(request, "Không thể cập nhật gian hàng.", "error")
                return RedirectResponse("/admin/categories", status_code=303)
            category.name_vi = normalized_name
            category.name_en = name_en.strip() or normalized_name
            category.position = position
            await session.commit()
        flash(request, "Đã lưu thông tin gian hàng.")
        return RedirectResponse("/admin/categories", status_code=303)

    @router.post("/admin/categories/{category_id}/toggle")
    async def toggle_category(
        category_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if valid_csrf(request, csrf):
            async with session_factory() as session:
                category = await session.get(Category, category_id)
                if category is not None:
                    category.active = not category.active
                    await session.commit()
                    flash(request, "Đã cập nhật trạng thái gian hàng.")
        return RedirectResponse("/admin/categories", status_code=303)

    @router.post("/admin/categories/{category_id}/delete")
    async def delete_category(
        category_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/categories", status_code=303)
        async with session_factory() as session:
            category = await session.get(Category, category_id)
            product_count = int(
                await session.scalar(
                    select(func.count(Product.id)).where(
                        Product.category_id == category_id,
                        Product.archived_at.is_(None),
                    )
                )
                or 0
            )
            if category is None:
                return RedirectResponse("/admin/categories", status_code=303)
            if product_count:
                flash(
                    request,
                    f"Gian hàng đang có {product_count} sản phẩm. Hãy chuyển hoặc xóa sản phẩm "
                    "trước khi xóa gian hàng.",
                    "error",
                )
                return RedirectResponse("/admin/categories", status_code=303)
            historical_product_count = int(
                await session.scalar(
                    select(func.count(Product.id)).where(Product.category_id == category_id)
                )
                or 0
            )
            if historical_product_count:
                category.active = False
                category.archived_at = datetime.now(UTC)
            else:
                await session.delete(category)
            await session.commit()
        flash(request, "Đã xóa gian hàng trống.")
        return RedirectResponse("/admin/categories", status_code=303)

    async def product_rows(session: AsyncSession) -> list[dict[str, object]]:
        stock_query = (
            select(
                InventoryItem.product_id,
                func.count(InventoryItem.id).label("stock"),
                func.avg(InventoryItem.cost_amount).label("average_cost"),
            )
            .where(InventoryItem.status == "available")
            .group_by(InventoryItem.product_id)
            .subquery()
        )
        coupon_query = (
            select(
                DiscountCode.product_id,
                func.count(DiscountCode.id).label("coupon_count"),
            )
            .where(DiscountCode.active.is_(True))
            .group_by(DiscountCode.product_id)
            .subquery()
        )
        rows = await session.execute(
            select(
                Product,
                Category,
                func.coalesce(stock_query.c.stock, 0),
                func.coalesce(stock_query.c.average_cost, 0),
                func.coalesce(coupon_query.c.coupon_count, 0),
            )
            .join(Category, Category.id == Product.category_id)
            .outerjoin(stock_query, stock_query.c.product_id == Product.id)
            .outerjoin(coupon_query, coupon_query.c.product_id == Product.id)
            .where(
                Product.archived_at.is_(None),
                Category.archived_at.is_(None),
                Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                Product.product_type == "account",
            )
            .order_by(Product.id.desc())
        )
        return [
            {
                "product": product,
                "category": category,
                "local_stock": int(stock),
                "api_stock": (
                    max(0, int(product.external_stock) - int(stock))
                    if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
                    else 0
                ),
                "source_stock": (
                    max(0, product.external_stock)
                    if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
                    else int(stock)
                ),
                "stock": (
                    0
                    if product.force_out_of_stock
                    else (
                        max(0, product.external_stock)
                        if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
                        else int(stock)
                    )
                ),
                "coupon_count": int(coupon_count),
                "api_sources": supplier_source_rows(product),
                "api_routes_enabled": bool(enabled_supplier_providers(product)),
                "stock_alert_mode": stock_alert_mode(product),
                "unit_cost": (
                    int(average_cost or 0)
                    if product.price_lock_enabled
                    else int(product.supplier_price or 0)
                    if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
                    else 0
                ),
                "unit_profit": (
                    product.price
                    - (
                        int(average_cost or 0)
                        if product.price_lock_enabled
                        else int(product.supplier_price or 0)
                    )
                    if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
                    else product.price
                ),
            }
            for product, category, stock, average_cost, coupon_count in rows
        ]

    @router.get("/admin/products", response_class=HTMLResponse)
    async def products_page(request: Request) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        async with session_factory() as session:
            products = await product_rows(session)
            categories = list(
                await session.scalars(
                    select(Category)
                    .where(Category.archived_at.is_(None))
                    .order_by(Category.position, Category.id)
                )
            )
        for product_row in products:
            product = product_row["product"]
            if not is_multi_supplier_product(
                product.fulfillment_source,
                product.supplier_product_id,
            ):
                continue
            enabled_providers = enabled_supplier_providers(product)
            fetched = await multi_source_route_fetch(product)
            if fetched is not None:
                routes = {route.provider: route for route in fetched.routes}
                failures = {failure.provider: failure for failure in fetched.failures}
                selected_plan = plan_supplier_routes(fetched.routes, 1)
                selected_provider = (
                    selected_plan[0][0].provider if selected_plan else None
                )
                source_rows = []
                for provider in configured_supplier_providers(
                    product.fulfillment_source,
                    product.supplier_product_id,
                ):
                    route = routes.get(provider)
                    source_rows.append(
                        {
                            "provider": provider,
                            "label": SUPPLIER_PROVIDER_LABELS[provider],
                            "enabled": provider in enabled_providers,
                            "stock": (
                                max(0, int(route.snapshot.effective_stock))
                                if route is not None
                                else None
                            ),
                            "failed": provider in failures,
                            "selected": provider == selected_provider,
                        }
                    )
                product_row["supplier_sources"] = source_rows
                configured_count = len(
                    configured_supplier_providers(
                        product.fulfillment_source,
                        product.supplier_product_id,
                    )
                )
                product_row["active_supplier_label"] = (
                    SUPPLIER_PROVIDER_LABELS.get(
                        selected_provider,
                        (
                            "Đã tắt cả hai"
                            if not enabled_providers and configured_count == 2
                            else "Đã tắt tất cả"
                            if not enabled_providers
                            else "Chưa có nguồn"
                        ),
                    )
                )
                if len(routes) == fetched.configured_count:
                    live_stock = sum(
                        max(0, int(route.snapshot.effective_stock))
                        for route in fetched.routes
                    ) + int(product_row["local_stock"])
                    product_row["source_stock"] = live_stock
                    product_row["stock"] = (
                        0 if product.force_out_of_stock else live_stock
                    )
        return templates.TemplateResponse(
            request,
            "products.html",
            page_context(
                request,
                "Sản phẩm",
                "products",
                products=products,
                categories=categories,
            ),
        )

    @router.post("/admin/products")
    async def create_product(
        request: Request,
        csrf: str = Form(...),
        category_id: int = Form(...),
        name_vi: str = Form(...),
        name_en: str = Form(""),
        price: str = Form(...),
        description_vi: str = Form(""),
        description_en: str = Form(""),
        product_type: str = Form("account"),
        fulfillment_source: str = Form("local"),
        supplier_product_id: str = Form(""),
        supplier_markup: str = Form("0"),
        notify_stock_without_balance_topup: str | None = Form(None),
        sale_notifications_enabled: str | None = Form(None),
        stock_notifications_enabled: str | None = Form(None),
        allow_quantity: str | None = Form(None),
        max_quantity: int = Form(10),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            flash(request, "Phiên biểu mẫu không hợp lệ.", "error")
            return RedirectResponse("/admin/products", status_code=303)
        parsed_price = parse_vnd(price)
        normalized_name = name_vi.strip()
        normalized_type = normalize_product_type(product_type)
        normalized_source = normalize_fulfillment_source(fulfillment_source)
        parsed_markup = parse_vnd(supplier_markup) or 0
        normalized_supplier_id = supplier_product_id.strip() or None
        if (
            not normalized_name
            or not parsed_price
            or parsed_price <= 0
            or normalized_type is None
            or normalized_source is None
            or (
                normalized_source in EXTERNAL_FULFILLMENT_SOURCES
                and not normalized_supplier_id
            )
        ):
            flash(request, "Thông tin sản phẩm không hợp lệ.", "error")
            return RedirectResponse("/admin/products", status_code=303)
        async with session_factory() as session:
            category = await session.get(Category, category_id)
            if category is None or category.archived_at is not None:
                flash(request, "Gian hàng không tồn tại.", "error")
                return RedirectResponse("/admin/products", status_code=303)
            session.add(
                Product(
                    category_id=category_id,
                    name_vi=normalized_name,
                    name_en=name_en.strip() or normalized_name,
                    description_vi=description_vi.strip(),
                    description_en=description_en.strip() or description_vi.strip(),
                    price=parsed_price,
                    product_type=normalized_type,
                    fulfillment_source=normalized_source,
                    supplier_product_id=(
                        normalized_supplier_id
                        if normalized_source in EXTERNAL_FULFILLMENT_SOURCES
                        else None
                    ),
                    supplier_markup=(
                        parsed_markup
                        if normalized_source in EXTERNAL_FULFILLMENT_SOURCES
                        else 0
                    ),
                    supplier_price=None,
                    external_stock=0,
                    sumistore_api_enabled=True,
                    lehai_api_enabled=True,
                    canboso_api_enabled=True,
                    notify_stock_without_balance_topup=(
                        notify_stock_without_balance_topup is not None
                        and normalized_source in EXTERNAL_FULFILLMENT_SOURCES
                    ),
                    sale_notifications_enabled=sale_notifications_enabled is not None,
                    stock_notifications_enabled=stock_notifications_enabled is not None,
                    allow_quantity=allow_quantity is not None,
                    max_quantity=max(1, min(max_quantity, 100)),
                )
            )
            await session.commit()
        flash(request, "Đã thêm sản phẩm.")
        return RedirectResponse("/admin/products", status_code=303)

    @router.get("/admin/products/{product_id}", response_class=HTMLResponse)
    async def edit_product_page(product_id: int, request: Request) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        async with session_factory() as session:
            product = await session.get(Product, product_id)
            categories = list(
                await session.scalars(
                    select(Category)
                    .where(Category.archived_at.is_(None))
                    .order_by(Category.position, Category.id)
                )
            )
            local_stock = (
                int(
                    await session.scalar(
                        select(func.count(InventoryItem.id)).where(
                            InventoryItem.product_id == product_id,
                            InventoryItem.status == "available",
                        )
                    )
                    or 0
                )
                if product is not None
                else 0
            )
        if (
            product is None
            or product.archived_at is not None
            or product.fulfillment_source not in SELLABLE_FULFILLMENT_SOURCES
            or product.product_type != "account"
        ):
            return RedirectResponse("/admin/products", status_code=303)
        return templates.TemplateResponse(
            request,
            "product_edit.html",
            page_context(
                request,
                "Sửa sản phẩm",
                "products",
                product=product,
                categories=categories,
                api_sources=supplier_source_rows(product),
                api_routes_enabled=bool(enabled_supplier_providers(product)),
                local_stock=local_stock,
                stock_alert_mode=stock_alert_mode(product),
            ),
        )

    @router.post("/admin/products/{product_id}")
    async def update_product(
        product_id: int,
        request: Request,
        csrf: str = Form(...),
        category_id: int = Form(...),
        name_vi: str = Form(...),
        name_en: str = Form(""),
        price: str = Form(...),
        description_vi: str = Form(""),
        description_en: str = Form(""),
        product_type: str = Form("account"),
        fulfillment_source: str = Form("local"),
        supplier_product_id: str = Form(""),
        supplier_markup: str | None = Form(None),
        api_source_controls_present: str | None = Form(None),
        sumistore_api_enabled: str | None = Form(None),
        lehai_api_enabled: str | None = Form(None),
        canboso_api_enabled: str | None = Form(None),
        notification_controls_present: str | None = Form(None),
        sale_notifications_enabled: str | None = Form(None),
        stock_notifications_enabled: str | None = Form(None),
        notify_stock_without_balance_topup: str | None = Form(None),
        allow_quantity: str | None = Form(None),
        max_quantity: int = Form(10),
        active: str | None = Form(None),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse(f"/admin/products/{product_id}", status_code=303)
        parsed_price = parse_vnd(price)
        normalized_name = name_vi.strip()
        normalized_type = normalize_product_type(product_type)
        normalized_source = normalize_fulfillment_source(fulfillment_source)
        parsed_markup = (
            parse_vnd(supplier_markup) or 0 if supplier_markup is not None else None
        )
        normalized_supplier_id = supplier_product_id.strip() or None
        async with session_factory() as session:
            product = await session.scalar(
                select(Product).where(Product.id == product_id).with_for_update()
            )
            category = await session.get(Category, category_id)
            if (
                product is None
                or category is None
                or product.archived_at is not None
                or category.archived_at is not None
                or not normalized_name
                or not parsed_price
                or normalized_type is None
                or normalized_source is None
                or (
                    normalized_source in EXTERNAL_FULFILLMENT_SOURCES
                    and not normalized_supplier_id
                )
            ):
                flash(request, "Không thể cập nhật sản phẩm.", "error")
                return RedirectResponse("/admin/products", status_code=303)
            old_source = product.fulfillment_source
            old_supplier_id = product.supplier_product_id
            if (
                old_source in EXTERNAL_FULFILLMENT_SOURCES
                and old_supplier_id
                and normalized_source == "local"
            ):
                # API products already support local inventory and always consume it first.
                # Keep their routing identity; the per-provider switches are the safe local-only mode.
                normalized_source = old_source
                normalized_supplier_id = old_supplier_id
            old_configured = set(
                configured_supplier_providers(old_source, old_supplier_id)
            )
            old_enabled = enabled_supplier_providers(product)
            product.category_id = category_id
            product.name_vi = normalized_name
            product.name_en = name_en.strip() or normalized_name
            product.price = parsed_price
            product.description_vi = description_vi.strip()
            product.description_en = description_en.strip() or description_vi.strip()
            product.product_type = normalized_type
            product.fulfillment_source = normalized_source
            product.supplier_product_id = (
                normalized_supplier_id
                if normalized_source in EXTERNAL_FULFILLMENT_SOURCES
                else None
            )
            new_configured = set(
                configured_supplier_providers(
                    product.fulfillment_source,
                    product.supplier_product_id,
                )
            )
            submitted_enabled = {
                "sumistore": sumistore_api_enabled is not None,
                "lehai": lehai_api_enabled is not None,
                "canboso": canboso_api_enabled is not None,
            }
            product.sumistore_api_enabled = (
                (
                    submitted_enabled["sumistore"]
                    if api_source_controls_present is not None
                    else product.sumistore_api_enabled
                )
                if "sumistore" in new_configured and "sumistore" in old_configured
                else True
            )
            product.lehai_api_enabled = (
                (
                    submitted_enabled["lehai"]
                    if api_source_controls_present is not None
                    else product.lehai_api_enabled
                )
                if "lehai" in new_configured and "lehai" in old_configured
                else True
            )
            product.canboso_api_enabled = (
                (
                    submitted_enabled["canboso"]
                    if api_source_controls_present is not None
                    else product.canboso_api_enabled
                )
                if "canboso" in new_configured and "canboso" in old_configured
                else True
            )
            new_enabled = enabled_supplier_providers(product)
            if normalized_source == "local":
                product.supplier_markup = 0
            elif new_enabled and parsed_markup is not None:
                product.supplier_markup = parsed_markup
            if normalized_source == "local":
                product.supplier_price = None
                product.external_stock = 0
                product.price_lock_enabled = False
            product.notify_stock_without_balance_topup = (
                notify_stock_without_balance_topup is not None
                and normalized_source in EXTERNAL_FULFILLMENT_SOURCES
            )
            if notification_controls_present is not None:
                product.sale_notifications_enabled = sale_notifications_enabled is not None
                product.stock_notifications_enabled = stock_notifications_enabled is not None
            if getattr(product, "sale_notifications_enabled", True) is False:
                await session.execute(
                    update(ProductPriceAlert)
                    .where(
                        ProductPriceAlert.product_id == product.id,
                        ProductPriceAlert.status.in_(("pending", "sending")),
                    )
                    .values(status="superseded")
                )
            if getattr(product, "stock_notifications_enabled", True) is False:
                await session.execute(
                    update(ProductStockAlert)
                    .where(
                        ProductStockAlert.product_id == product.id,
                        ProductStockAlert.status.in_(("pending", "sending")),
                    )
                    .values(status="superseded")
                )
            product.allow_quantity = allow_quantity is not None
            product.max_quantity = max(1, min(max_quantity, 100))
            product.active = active is not None
            routes_changed = (
                old_source != product.fulfillment_source
                or old_supplier_id != product.supplier_product_id
                or old_enabled != enabled_supplier_providers(product)
            )
            if (
                routes_changed
                and product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
            ):
                local_stock = int(
                    await session.scalar(
                        select(func.count(InventoryItem.id)).where(
                            InventoryItem.product_id == product.id,
                            InventoryItem.status == "available",
                        )
                    )
                    or 0
                )
                # Remove the old combined snapshot before fetching only the
                # newly enabled routes, so disabled stock cannot linger.
                product.external_stock = local_stock
                product.supplier_available_stock = 0
                product.supplier_available_stock_initialized = False
                product.supplier_owner_balance = None
                product.supplier_synced_at = None
                await refresh_product_from_supplier(
                    session,
                    product,
                    supplier_client,
                    lehai_client,
                    canboso_client,
                    nce_client,
                    haji_client,
                )
            active_campaign = await session.scalar(
                select(FlashSaleCampaign)
                .where(
                    FlashSaleCampaign.product_id == product.id,
                    FlashSaleCampaign.status == "active",
                )
                .with_for_update()
            )
            if active_campaign is not None and active_campaign.sale_price >= parsed_price:
                active_campaign.status = "price_invalid"
                active_campaign.ended_at = datetime.now(UTC)
                if active_campaign.notification_status in {"pending", "sending"}:
                    active_campaign.notification_status = "superseded"
            await session.commit()
        flash(request, "Đã lưu thông tin sản phẩm.")
        return RedirectResponse(f"/admin/products/{product_id}", status_code=303)

    @router.post("/admin/products/{product_id}/stock-zero")
    async def toggle_product_stock_zero(
        product_id: int,
        request: Request,
        csrf: str = Form(...),
        action: str = Form("zero"),
        return_to: str = Form("list"),
    ) -> RedirectResponse:
        redirect_url = (
            f"/admin/products/{product_id}"
            if return_to == "edit"
            else "/admin/products"
        )
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            flash(request, "Phiên biểu mẫu không hợp lệ.", "error")
            return RedirectResponse(redirect_url, status_code=303)
        async with session_factory() as session:
            product = await session.scalar(
                select(Product).where(Product.id == product_id).with_for_update()
            )
            if (
                product is None
                or product.archived_at is not None
                or product.product_type != "account"
                or product.fulfillment_source not in SELLABLE_FULFILLMENT_SOURCES
            ):
                flash(request, "Sản phẩm không tồn tại.", "error")
                return RedirectResponse("/admin/products", status_code=303)

            restore = action == "restore"
            product.force_out_of_stock = not restore
            if not restore:
                await session.execute(
                    update(ProductPriceAlert)
                    .where(
                        ProductPriceAlert.product_id == product.id,
                        ProductPriceAlert.status.in_(("pending", "sending")),
                    )
                    .values(status="superseded")
                )
                await session.execute(
                    update(ProductStockAlert)
                    .where(
                        ProductStockAlert.product_id == product.id,
                        ProductStockAlert.status.in_(("pending", "sending")),
                    )
                    .values(status="superseded")
                )
                await session.execute(
                    update(FlashSaleCampaign)
                    .where(
                        FlashSaleCampaign.product_id == product.id,
                        FlashSaleCampaign.notification_status.in_(("pending", "sending")),
                    )
                    .values(notification_status="superseded")
                )
            await session.commit()
            product_name = product.name_vi

        if restore:
            flash(request, f"Đã mở bán lại {product_name}; tồn kho được giữ nguyên.")
        else:
            flash(
                request,
                f"Đã đưa {product_name} về 0 hàng. Kho thật vẫn được giữ để mở lại sau.",
            )
        return RedirectResponse(redirect_url, status_code=303)

    @router.post("/admin/products/{product_id}/delete")
    async def delete_product(
        product_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse(f"/admin/products/{product_id}", status_code=303)
        async with session_factory() as session:
            product = await session.get(Product, product_id)
            order_count = int(
                await session.scalar(
                    select(func.count(Order.id)).where(Order.product_id == product_id)
                )
                or 0
            )
            payment_count = int(
                await session.scalar(
                    select(func.count(Deposit.id)).where(Deposit.product_id == product_id)
                )
                or 0
            )
            if product is None:
                return RedirectResponse("/admin/products", status_code=303)
            if order_count or payment_count:
                now = datetime.now(UTC)
                product.active = False
                product.archived_at = now
                product.force_out_of_stock = True
                product.sale_notifications_enabled = False
                product.stock_notifications_enabled = False
                await session.execute(
                    delete(InventoryItem).where(
                        InventoryItem.product_id == product_id,
                        InventoryItem.status == "available",
                    )
                )
                await session.execute(
                    update(DiscountCode)
                    .where(DiscountCode.product_id == product_id)
                    .values(active=False)
                )
                await session.execute(
                    update(QuantityDiscount)
                    .where(QuantityDiscount.product_id == product_id)
                    .values(active=False)
                )
                await session.execute(
                    update(ProductPriceAlert)
                    .where(
                        ProductPriceAlert.product_id == product_id,
                        ProductPriceAlert.status.in_(("pending", "sending")),
                    )
                    .values(status="superseded")
                )
                await session.execute(
                    update(ProductStockAlert)
                    .where(
                        ProductStockAlert.product_id == product_id,
                        ProductStockAlert.status.in_(("pending", "sending")),
                    )
                    .values(status="superseded")
                )
                await session.execute(
                    update(FlashSaleCampaign)
                    .where(
                        FlashSaleCampaign.product_id == product_id,
                        FlashSaleCampaign.status == "active",
                    )
                    .values(
                        status="cancelled",
                        ended_at=now,
                        notification_status="superseded",
                    )
                )
                await session.commit()
                flash(
                    request,
                    "Đã xóa sản phẩm khỏi vận hành; lịch sử đơn và thanh toán vẫn được giữ nguyên.",
                )
                return RedirectResponse("/admin/products", status_code=303)
            await session.execute(
                delete(InventoryItem).where(InventoryItem.product_id == product_id)
            )
            await session.execute(
                delete(DiscountCode).where(DiscountCode.product_id == product_id)
            )
            await session.execute(
                delete(QuantityDiscount).where(QuantityDiscount.product_id == product_id)
            )
            await session.delete(product)
            await session.commit()
        flash(request, "Đã xóa sản phẩm và toàn bộ kho chưa bán của sản phẩm đó.")
        return RedirectResponse("/admin/products", status_code=303)

    @router.get("/admin/flash-sales", response_class=HTMLResponse)
    async def flash_sales_page(request: Request, page: int = 1) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        async with session_factory() as session:
            products = [
                row
                for row in await product_rows(session)
                if row["product"].active and int(row["stock"]) > 0
            ]
            campaign_count = int(
                await session.scalar(select(func.count(FlashSaleCampaign.id))) or 0
            )
            pager = admin_pager(request, campaign_count, page)
            campaign_records = (
                await session.execute(
                    select(FlashSaleCampaign, Product)
                    .join(Product, Product.id == FlashSaleCampaign.product_id)
                    .order_by(FlashSaleCampaign.id.desc())
                    .offset(pager.offset)
                    .limit(ADMIN_PAGE_SIZE)
                )
            ).all()
            campaign_ids = [campaign.id for campaign, _product in campaign_records]
            failure_groups: dict[int, list[dict[str, object]]] = {}
            if campaign_ids:
                for campaign_id, error, count in await session.execute(
                    select(
                        ProductAlertDelivery.alert_id,
                        ProductAlertDelivery.last_error,
                        func.count(ProductAlertDelivery.id),
                    )
                    .where(
                        ProductAlertDelivery.alert_type == "flash",
                        ProductAlertDelivery.alert_id.in_(campaign_ids),
                        ProductAlertDelivery.status == "failed",
                    )
                    .group_by(
                        ProductAlertDelivery.alert_id,
                        ProductAlertDelivery.last_error,
                    )
                ):
                    failure_groups.setdefault(int(campaign_id), []).append(
                        {
                            "error": error or "Không rõ lỗi",
                            "count": int(count),
                        }
                    )

            now = datetime.now(UTC)
            campaigns = []
            for campaign, product in campaign_records:
                started_at = campaign.notification_started_at
                completed_at = campaign.notification_completed_at
                if started_at is not None and started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
                if completed_at is not None and completed_at.tzinfo is None:
                    completed_at = completed_at.replace(tzinfo=UTC)
                processed = campaign.delivered_count + campaign.failed_count
                elapsed_seconds = 0
                if started_at is not None:
                    elapsed_seconds = max(
                        0,
                        int(((completed_at or now) - started_at).total_seconds()),
                    )
                if elapsed_seconds >= 60:
                    minutes, seconds = divmod(elapsed_seconds, 60)
                    duration = f"{minutes}p {seconds}s"
                elif started_at is not None:
                    duration = f"{elapsed_seconds}s"
                else:
                    duration = "—"
                campaigns.append(
                    {
                        "campaign": campaign,
                        "product": product,
                        "remaining_quantity": max(
                            0,
                            campaign.total_quantity
                            - campaign.sold_quantity
                            - campaign.reserved_quantity,
                        ),
                        "processed": processed,
                        "notification_remaining": max(
                            0,
                            campaign.total_recipients - processed,
                        ),
                        "speed": (
                            round(processed / elapsed_seconds, 1)
                            if elapsed_seconds > 0
                            else 0
                        ),
                        "duration": duration,
                        "failures": failure_groups.get(campaign.id, []),
                    }
                )

            active_count = int(
                await session.scalar(
                    select(func.count(FlashSaleCampaign.id)).where(
                        FlashSaleCampaign.status == "active"
                    )
                )
                or 0
            )
            sold_quantity = int(
                await session.scalar(
                    select(func.coalesce(func.sum(FlashSaleCampaign.sold_quantity), 0))
                )
                or 0
            )
            reserved_quantity = int(
                await session.scalar(
                    select(func.coalesce(func.sum(FlashSaleCampaign.reserved_quantity), 0))
                )
                or 0
            )
            notification_active = int(
                await session.scalar(
                    select(func.count(FlashSaleCampaign.id)).where(
                        FlashSaleCampaign.notification_status.in_(("pending", "sending"))
                    )
                )
                or 0
            )
        return templates.TemplateResponse(
            request,
            "flash_sales.html",
            page_context(
                request,
                "Flash Sale",
                "flash-sales",
                products=products,
                campaigns=campaigns,
                campaign_count=campaign_count,
                active_count=active_count,
                sold_quantity=sold_quantity,
                reserved_quantity=reserved_quantity,
                notification_active=notification_active,
                pager=pager,
                auto_refresh=notification_active > 0,
            ),
        )

    @router.post("/admin/flash-sales")
    async def create_flash_sale(
        request: Request,
        csrf: str = Form(...),
        product_id: int = Form(...),
        sale_price: str = Form(...),
        total_quantity: int = Form(...),
        message_text: str = Form(""),
        image: UploadFile | None = File(default=None),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            flash(request, "Phiên biểu mẫu không hợp lệ.", "error")
            return RedirectResponse("/admin/flash-sales", status_code=303)
        parsed_sale_price = parse_vnd(sale_price) or 0
        custom_message = message_text.strip()
        if parsed_sale_price <= 0 or total_quantity <= 0 or total_quantity > 100_000:
            flash(request, "Giá sale hoặc số lượng sale không hợp lệ.", "error")
            return RedirectResponse("/admin/flash-sales", status_code=303)
        message_limit = 1024 if image is not None and image.filename else 4096
        if custom_message and len(custom_message) > message_limit:
            flash(
                request,
                f"Nội dung thông báo tối đa {message_limit} ký tự với lựa chọn hiện tại.",
                "error",
            )
            return RedirectResponse("/admin/flash-sales", status_code=303)
        try:
            telegram_photo_file_id = await upload_flash_sale_image(image)
        except ValueError as exc:
            flash(request, str(exc), "error")
            return RedirectResponse("/admin/flash-sales", status_code=303)

        async with session_factory() as session:
            async with session.begin():
                product = await session.scalar(
                    select(Product).where(Product.id == product_id).with_for_update()
                )
                if (
                    product is None
                    or not product.active
                    or product.archived_at is not None
                    or product.product_type != "account"
                    or product.fulfillment_source not in SELLABLE_FULFILLMENT_SOURCES
                    or parsed_sale_price >= product.price
                ):
                    flash(
                        request,
                        "Sản phẩm không hợp lệ; giá sale phải lớn hơn 0 và thấp hơn "
                        "giá bán hiện tại.",
                        "error",
                    )
                    return RedirectResponse("/admin/flash-sales", status_code=303)
                existing = await session.scalar(
                    select(FlashSaleCampaign)
                    .where(
                        FlashSaleCampaign.product_id == product.id,
                        or_(
                            FlashSaleCampaign.status == "active",
                            FlashSaleCampaign.reserved_quantity > 0,
                        ),
                    )
                    .with_for_update()
                )
                if existing is not None:
                    flash(request, "Sản phẩm này đang có một chiến dịch Flash Sale.", "error")
                    return RedirectResponse("/admin/flash-sales", status_code=303)
                if product.force_out_of_stock:
                    stock = 0
                elif product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
                    stock = max(0, product.external_stock)
                else:
                    stock = int(
                        await session.scalar(
                            select(func.count(InventoryItem.id)).where(
                                InventoryItem.product_id == product.id,
                                InventoryItem.status == "available",
                            )
                        )
                        or 0
                    )
                if stock < total_quantity:
                    flash(
                        request,
                        f"Kho hiện chỉ có {stock} sản phẩm, không đủ {total_quantity} suất sale.",
                        "error",
                    )
                    return RedirectResponse("/admin/flash-sales", status_code=303)
                campaign_message = custom_message or default_flash_sale_message(
                    product.name_vi,
                    product.price,
                    parsed_sale_price,
                    total_quantity,
                )
                session.add(
                    FlashSaleCampaign(
                        product_id=product.id,
                        original_price=product.price,
                        sale_price=parsed_sale_price,
                        supplier_price_at_start=(
                            int(product.supplier_price)
                            if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
                            and product.supplier_price is not None
                            else None
                        ),
                        total_quantity=total_quantity,
                        message_text=campaign_message,
                        telegram_photo_file_id=telegram_photo_file_id,
                        created_by=str(request.session.get("dashboard_admin") or "admin"),
                    )
                )
        flash(
            request,
            "Đã bật Flash Sale. Thông báo đang được xếp hàng gửi tới khách hàng.",
        )
        return RedirectResponse("/admin/flash-sales", status_code=303)

    @router.post("/admin/flash-sales/{campaign_id}/cancel")
    async def cancel_flash_sale(
        campaign_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/flash-sales", status_code=303)
        async with session_factory() as session:
            async with session.begin():
                campaign = await session.scalar(
                    select(FlashSaleCampaign)
                    .where(FlashSaleCampaign.id == campaign_id)
                    .with_for_update()
                )
                if campaign is None or campaign.status != "active":
                    flash(request, "Chiến dịch không còn hoạt động.", "error")
                    return RedirectResponse("/admin/flash-sales", status_code=303)
                campaign.status = "cancelled"
                campaign.ended_at = datetime.now(UTC)
                if campaign.notification_status in {"pending", "sending"}:
                    campaign.notification_status = "superseded"
        flash(request, "Đã dừng Flash Sale; sản phẩm lập tức trở về giá thường.")
        return RedirectResponse("/admin/flash-sales", status_code=303)

    @router.get("/admin/discounts", response_class=HTMLResponse)
    async def discounts_page(request: Request, product_id: int | None = None) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        async with session_factory() as session:
            products = list(
                await session.scalars(
                    select(Product)
                    .where(
                        Product.archived_at.is_(None),
                        Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                        Product.product_type == "account",
                    )
                    .order_by(Product.name_vi, Product.id)
                )
            )
            statement = (
                select(DiscountCode, Product)
                .join(Product, Product.id == DiscountCode.product_id)
                .where(
                    Product.archived_at.is_(None),
                    Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                    Product.product_type == "account",
                )
                .order_by(DiscountCode.id.desc())
            )
            if product_id is not None:
                statement = statement.where(DiscountCode.product_id == product_id)
            codes = [
                {"code": code, "product": product}
                for code, product in await session.execute(statement)
            ]
            quantity_statement = (
                select(QuantityDiscount, Product)
                .join(Product, Product.id == QuantityDiscount.product_id)
                .where(
                    Product.archived_at.is_(None),
                    Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                    Product.product_type == "account",
                )
                .order_by(
                    Product.name_vi,
                    QuantityDiscount.min_quantity,
                )
            )
            if product_id is not None:
                quantity_statement = quantity_statement.where(
                    QuantityDiscount.product_id == product_id
                )
            quantity_tiers = [
                {"tier": tier, "product": product}
                for tier, product in await session.execute(
                    quantity_statement
                )
            ]
            active_count = int(
                await session.scalar(
                    select(func.count(DiscountCode.id))
                    .join(Product, Product.id == DiscountCode.product_id)
                    .where(
                        DiscountCode.active.is_(True),
                        Product.archived_at.is_(None),
                        Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                        Product.product_type == "account",
                    )
                )
                or 0
            )
            total_uses = int(
                await session.scalar(
                    select(func.coalesce(func.sum(DiscountCode.used_count), 0))
                    .join(Product, Product.id == DiscountCode.product_id)
                    .where(
                        Product.archived_at.is_(None),
                        Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                        Product.product_type == "account",
                    )
                )
                or 0
            )
            total_discount = int(
                await session.scalar(
                    select(func.coalesce(func.sum(Order.discount_amount), 0))
                )
                or 0
            )
            active_quantity_tiers = int(
                await session.scalar(
                    select(func.count(QuantityDiscount.id))
                    .join(Product, Product.id == QuantityDiscount.product_id)
                    .where(
                        QuantityDiscount.active.is_(True),
                        Product.archived_at.is_(None),
                        Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                        Product.product_type == "account",
                    )
                )
                or 0
            )
        return templates.TemplateResponse(
            request,
            "discounts.html",
            page_context(
                request,
                "Mã giảm giá",
                "discounts",
                products=products,
                codes=codes,
                quantity_tiers=quantity_tiers,
                selected_product_id=product_id,
                stats={
                    "active": active_count,
                    "uses": total_uses,
                    "discount": total_discount,
                    "quantity_tiers": active_quantity_tiers,
                },
            ),
        )

    @router.post("/admin/quantity-discounts")
    async def create_quantity_discount(
        request: Request,
        csrf: str = Form(...),
        product_id: int = Form(...),
        min_quantity: list[int] = Form(...),
        discount_percent: list[int] = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/discounts", status_code=303)
        if (
            not min_quantity
            or len(min_quantity) != len(discount_percent)
            or len(min_quantity) > 20
        ):
            flash(request, "Danh sách mốc giảm giá không hợp lệ.", "error")
            return RedirectResponse("/admin/discounts", status_code=303)
        tiers = sorted(zip(min_quantity, discount_percent, strict=True))
        thresholds = [threshold for threshold, _percent in tiers]
        if len(set(thresholds)) != len(thresholds):
            flash(request, "Không thể nhập hai mốc số lượng giống nhau.", "error")
            return RedirectResponse("/admin/discounts", status_code=303)
        async with session_factory() as session:
            product = await session.get(Product, product_id)
            if (
                product is None
                or product.archived_at is not None
                or product.fulfillment_source not in SELLABLE_FULFILLMENT_SOURCES
                or product.product_type != "account"
            ):
                flash(request, "Sản phẩm không hợp lệ.", "error")
                return RedirectResponse("/admin/discounts", status_code=303)
            if any(
                threshold < 2
                or threshold > product.max_quantity
                or not 1 <= percent <= 99
                for threshold, percent in tiers
            ):
                flash(
                    request,
                    "Mốc số lượng vượt giới hạn mua hoặc phần trăm giảm không hợp lệ.",
                    "error",
                )
                return RedirectResponse("/admin/discounts", status_code=303)
            existing_thresholds = set(
                await session.scalars(
                    select(QuantityDiscount.min_quantity).where(
                        QuantityDiscount.product_id == product.id,
                        QuantityDiscount.min_quantity.in_(thresholds),
                    )
                )
            )
            if existing_thresholds:
                duplicate_text = ", ".join(str(value) for value in sorted(existing_thresholds))
                flash(
                    request,
                    f"Các mốc {duplicate_text} đã tồn tại cho sản phẩm này.",
                    "error",
                )
                return RedirectResponse("/admin/discounts", status_code=303)
            session.add_all(
                [
                    QuantityDiscount(
                        product_id=product.id,
                        min_quantity=threshold,
                        discount_type="percent",
                        discount_percent=percent,
                        discount_amount=0,
                    )
                    for threshold, percent in tiers
                ]
            )
            await session.commit()
        flash(
            request,
            f"Đã thêm {len(tiers)} mốc ưu đãi số lượng cho {product.name_vi}.",
        )
        return RedirectResponse("/admin/discounts", status_code=303)

    @router.post("/admin/quantity-discounts/fixed")
    async def create_fixed_quantity_discount(
        request: Request,
        csrf: str = Form(...),
        product_id: int = Form(...),
        min_quantity: list[int] = Form(...),
        discount_amount: list[int] = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/discounts", status_code=303)
        if (
            not min_quantity
            or len(min_quantity) != len(discount_amount)
            or len(min_quantity) > 20
        ):
            flash(request, "Danh sách mốc giảm tiền không hợp lệ.", "error")
            return RedirectResponse("/admin/discounts", status_code=303)
        tiers = sorted(zip(min_quantity, discount_amount, strict=True))
        thresholds = [threshold for threshold, _amount in tiers]
        if len(set(thresholds)) != len(thresholds):
            flash(request, "Không thể nhập hai mốc số lượng giống nhau.", "error")
            return RedirectResponse("/admin/discounts", status_code=303)
        async with session_factory() as session:
            product = await session.get(Product, product_id)
            if (
                product is None
                or product.archived_at is not None
                or product.fulfillment_source not in SELLABLE_FULFILLMENT_SOURCES
                or product.product_type != "account"
            ):
                flash(request, "Sản phẩm không hợp lệ.", "error")
                return RedirectResponse("/admin/discounts", status_code=303)
            if any(
                threshold < 2
                or threshold > product.max_quantity
                or amount < 1
                or amount >= product.price
                for threshold, amount in tiers
            ):
                flash(
                    request,
                    "Mốc số lượng vượt giới hạn mua hoặc số tiền giảm không hợp lệ.",
                    "error",
                )
                return RedirectResponse("/admin/discounts", status_code=303)
            existing_thresholds = set(
                await session.scalars(
                    select(QuantityDiscount.min_quantity).where(
                        QuantityDiscount.product_id == product.id,
                        QuantityDiscount.min_quantity.in_(thresholds),
                    )
                )
            )
            if existing_thresholds:
                duplicate_text = ", ".join(str(value) for value in sorted(existing_thresholds))
                flash(
                    request,
                    f"Các mốc {duplicate_text} đã tồn tại cho sản phẩm này.",
                    "error",
                )
                return RedirectResponse("/admin/discounts", status_code=303)
            session.add_all(
                [
                    QuantityDiscount(
                        product_id=product.id,
                        min_quantity=threshold,
                        discount_type="fixed",
                        discount_percent=0,
                        discount_amount=amount,
                    )
                    for threshold, amount in tiers
                ]
            )
            await session.commit()
        flash(
            request,
            f"Đã thêm {len(tiers)} mốc giảm tiền cố định cho {product.name_vi}.",
        )
        return RedirectResponse("/admin/discounts", status_code=303)

    @router.post("/admin/quantity-discounts/{tier_id}/toggle")
    async def toggle_quantity_discount(
        tier_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if valid_csrf(request, csrf):
            async with session_factory() as session:
                tier = await session.get(QuantityDiscount, tier_id)
                if tier is not None:
                    tier.active = not tier.active
                    await session.commit()
                    flash(request, "Đã cập nhật trạng thái ưu đãi số lượng.")
        return RedirectResponse("/admin/discounts", status_code=303)

    @router.post("/admin/quantity-discounts/{tier_id}/delete")
    async def delete_quantity_discount(
        tier_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/discounts", status_code=303)
        async with session_factory() as session:
            tier = await session.get(QuantityDiscount, tier_id)
            if tier is not None:
                await session.delete(tier)
                await session.commit()
                flash(request, "Đã xóa mốc ưu đãi số lượng.")
        return RedirectResponse("/admin/discounts", status_code=303)

    @router.post("/admin/discounts")
    async def create_discount(
        request: Request,
        csrf: str = Form(...),
        product_id: int = Form(...),
        code: str = Form(...),
        discount_type: str = Form("fixed"),
        discount_value: str = Form(...),
        max_uses: int = Form(0),
        starts_at: str = Form(""),
        expires_at: str = Form(""),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/discounts", status_code=303)
        normalized_code = code.strip().upper()
        normalized_type = discount_type.strip().lower()
        try:
            parsed_value = (
                int(discount_value.strip())
                if normalized_type == "percent"
                else int(parse_vnd(discount_value) or 0)
            )
        except ValueError:
            parsed_value = 0
        parsed_start = parse_local_datetime(starts_at)
        parsed_expiry = parse_local_datetime(expires_at)
        invalid_dates = bool(starts_at.strip() and parsed_start is None) or bool(
            expires_at.strip() and parsed_expiry is None
        )
        invalid_value = (
            normalized_type not in {"fixed", "percent"}
            or parsed_value <= 0
            or (normalized_type == "percent" and parsed_value >= 100)
        )
        if (
            not re.fullmatch(r"[A-Z0-9_-]{3,32}", normalized_code)
            or invalid_value
            or max_uses < 0
            or invalid_dates
            or (parsed_start and parsed_expiry and parsed_start >= parsed_expiry)
        ):
            flash(request, "Thông tin mã giảm giá không hợp lệ.", "error")
            return RedirectResponse("/admin/discounts", status_code=303)
        async with session_factory() as session:
            product = await session.get(Product, product_id)
            duplicate = await session.scalar(
                select(DiscountCode.id).where(DiscountCode.code == normalized_code)
            )
            if (
                product is None
                or product.archived_at is not None
                or product.fulfillment_source not in SELLABLE_FULFILLMENT_SOURCES
                or product.product_type != "account"
                or duplicate is not None
            ):
                flash(request, "Sản phẩm không tồn tại hoặc mã đã được sử dụng.", "error")
                return RedirectResponse("/admin/discounts", status_code=303)
            session.add(
                DiscountCode(
                    product_id=product.id,
                    code=normalized_code,
                    discount_type=normalized_type,
                    discount_value=parsed_value,
                    max_uses=max_uses,
                    starts_at=parsed_start,
                    expires_at=parsed_expiry,
                )
            )
            await session.commit()
        flash(request, f"Đã tạo mã {normalized_code} cho sản phẩm đã chọn.")
        return RedirectResponse("/admin/discounts", status_code=303)

    @router.post("/admin/discounts/{discount_id}/toggle")
    async def toggle_discount(
        discount_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if valid_csrf(request, csrf):
            async with session_factory() as session:
                coupon = await session.get(DiscountCode, discount_id)
                if coupon is not None:
                    coupon.active = not coupon.active
                    await session.commit()
                    flash(request, "Đã cập nhật trạng thái mã giảm giá.")
        return RedirectResponse("/admin/discounts", status_code=303)

    @router.post("/admin/discounts/{discount_id}/delete")
    async def delete_discount(
        discount_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/discounts", status_code=303)
        async with session_factory() as session:
            coupon = await session.get(DiscountCode, discount_id)
            reference_count = int(
                await session.scalar(
                    select(func.count(Order.id)).where(Order.discount_code_id == discount_id)
                )
                or 0
            ) + int(
                await session.scalar(
                    select(func.count(Deposit.id)).where(Deposit.discount_code_id == discount_id)
                )
                or 0
            )
            if coupon is None:
                return RedirectResponse("/admin/discounts", status_code=303)
            if reference_count:
                coupon.active = False
                await session.commit()
                flash(
                    request,
                    "Mã đã có lịch sử sử dụng nên được tắt thay vì xóa.",
                    "error",
                )
                return RedirectResponse("/admin/discounts", status_code=303)
            await session.delete(coupon)
            await session.commit()
        flash(request, "Đã xóa mã giảm giá chưa sử dụng.")
        return RedirectResponse("/admin/discounts", status_code=303)

    @router.get("/admin/inventory", response_class=HTMLResponse)
    async def inventory_page(request: Request, page: int = 1) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        async with session_factory() as session:
            products = await product_rows(session)
            inventory_conditions = (
                Product.archived_at.is_(None),
                Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                Product.product_type == "account",
            )
            inventory_count = int(
                await session.scalar(
                    select(func.count(InventoryItem.id))
                    .join(Product, Product.id == InventoryItem.product_id)
                    .where(*inventory_conditions)
                )
                or 0
            )
            pager = admin_pager(request, inventory_count, page)
            inventory_rows = await session.execute(
                select(InventoryItem, Product)
                .join(Product, Product.id == InventoryItem.product_id)
                .where(*inventory_conditions)
                .order_by(InventoryItem.id.desc())
                .offset(pager.offset)
                .limit(ADMIN_PAGE_SIZE)
            )
            recent_items = [{"item": item, "product": product} for item, product in inventory_rows]
            duplicate_alert_count = int(
                await session.scalar(select(func.count(InventoryDuplicateAlert.id))) or 0
            )
            duplicate_alerts = list(
                await session.scalars(
                    select(InventoryDuplicateAlert)
                    .order_by(InventoryDuplicateAlert.id.desc())
                    .limit(ADMIN_PAGE_SIZE)
                )
            )
            duplicate_product_ids = {alert.product_id for alert in duplicate_alerts}
            existing_item_ids = {
                alert.existing_inventory_item_id
                for alert in duplicate_alerts
                if alert.existing_inventory_item_id is not None
            }
            existing_items = {
                item.id: item
                for item in (
                    list(
                        await session.scalars(
                            select(InventoryItem).where(InventoryItem.id.in_(existing_item_ids))
                        )
                    )
                    if existing_item_ids
                    else []
                )
            }
            duplicate_product_ids.update(item.product_id for item in existing_items.values())
            duplicate_products = {
                product.id: product
                for product in (
                    list(
                        await session.scalars(
                            select(Product).where(Product.id.in_(duplicate_product_ids))
                        )
                    )
                    if duplicate_product_ids
                    else []
                )
            }
            duplicate_rows = []
            for alert in duplicate_alerts:
                try:
                    identifier = cipher.decrypt(alert.encrypted_identifier)
                except Exception:
                    identifier = "Không đọc được định danh"
                existing_item = (
                    existing_items.get(alert.existing_inventory_item_id)
                    if alert.existing_inventory_item_id is not None
                    else None
                )
                duplicate_rows.append(
                    {
                        "alert": alert,
                        "identifier": identifier,
                        "product": duplicate_products.get(alert.product_id),
                        "existing_item": existing_item,
                        "existing_product": (
                            duplicate_products.get(existing_item.product_id)
                            if existing_item is not None
                            else None
                        ),
                    }
                )
            withdrawal_results = await session.execute(
                select(
                    InventoryItem.withdrawal_code,
                    InventoryItem.product_id,
                    Product.name_vi,
                    func.count(InventoryItem.id),
                    func.max(InventoryItem.withdrawn_at),
                    func.max(InventoryItem.withdrawn_by),
                    func.max(InventoryItem.withdrawal_reason),
                    func.count(InventoryItem.supplier_provider),
                )
                .join(Product, Product.id == InventoryItem.product_id)
                .where(
                    InventoryItem.status == "withdrawn",
                    InventoryItem.withdrawal_code.is_not(None),
                )
                .group_by(
                    InventoryItem.withdrawal_code,
                    InventoryItem.product_id,
                    Product.name_vi,
                )
                .order_by(func.max(InventoryItem.withdrawn_at).desc())
                .limit(ADMIN_PAGE_SIZE)
            )
            recent_withdrawals = [
                {
                    "code": code,
                    "product_id": product_id,
                    "product_name": product_name,
                    "quantity": int(quantity),
                    "withdrawn_at": withdrawn_at,
                    "withdrawn_by": withdrawn_by,
                    "reason": reason,
                    "source": "API" if int(api_item_count) > 0 else "Kho nhập",
                }
                for (
                    code,
                    product_id,
                    product_name,
                    quantity,
                    withdrawn_at,
                    withdrawn_by,
                    reason,
                    api_item_count,
                ) in withdrawal_results
            ]
            pending_withdrawal_results = await session.execute(
                select(SupplierRecoveryRequest, Product)
                .join(Product, Product.id == SupplierRecoveryRequest.product_id)
                .where(
                    SupplierRecoveryRequest.inventory_withdrawal_code.is_not(None),
                    SupplierRecoveryRequest.status != "recovered",
                )
                .order_by(SupplierRecoveryRequest.id.desc())
                .limit(ADMIN_PAGE_SIZE)
            )
            pending_withdrawals = [
                {"recovery": recovery, "product": product}
                for recovery, product in pending_withdrawal_results
            ]
        return templates.TemplateResponse(
            request,
            "inventory.html",
            page_context(
                request,
                "Nhập kho",
                "inventory",
                products=products,
                import_products=products,
                withdrawal_products=[
                    row
                    for row in products
                    if int(row["local_stock"]) > 0
                    or (
                        row["product"].fulfillment_source
                        in EXTERNAL_FULFILLMENT_SOURCES
                        and bool(row["api_routes_enabled"])
                    )
                ],
                recent_items=recent_items,
                recent_withdrawals=recent_withdrawals,
                pending_withdrawals=pending_withdrawals,
                duplicate_rows=duplicate_rows,
                duplicate_alert_count=duplicate_alert_count,
                pager=pager,
            ),
        )

    @router.post("/admin/inventory")
    async def add_inventory(
        request: Request,
        csrf: str = Form(...),
        product_id: int = Form(...),
        items: str = Form(...),
        cost_amount: str = Form(...),
        lock_sale_price: str | None = Form(None),
        notify_stock_arrival: str | None = Form(None),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/inventory", status_code=303)
        parsed_items = split_inventory_items(items)
        parsed_cost = parse_vnd(cost_amount)
        if parsed_cost is None or parsed_cost < 0:
            flash(request, "Giá vốn mỗi tài khoản không hợp lệ.", "error")
            return RedirectResponse("/admin/inventory", status_code=303)
        lock_applied = False
        duplicate_count = 0
        accepted_items: list[str] = []
        async with session_factory() as session:
            product = await session.scalar(
                select(Product).where(Product.id == product_id).with_for_update()
            )
            if (
                product is None
                or product.archived_at is not None
                or product.fulfillment_source not in SELLABLE_FULFILLMENT_SOURCES
                or product.product_type != "account"
                or not parsed_items
            ):
                flash(request, "Sản phẩm hoặc dữ liệu kho không hợp lệ.", "error")
                return RedirectResponse("/admin/inventory", status_code=303)
            duplicate_check = await filter_duplicate_inventory(
                session,
                cipher,
                product_id=product.id,
                raw_items=parsed_items,
            )
            duplicate_count = duplicate_check.duplicate_count
            accepted_items = [candidate.raw_item for candidate in duplicate_check.accepted]
            if not accepted_items:
                await session.commit()
                flash(
                    request,
                    f"Không có tài khoản sạch để nhập; đã bỏ qua {duplicate_count} "
                    "tài khoản nghi ngờ/trùng. Xem bảng cảnh báo bên dưới.",
                    "error",
                )
                return RedirectResponse("/admin/inventory", status_code=303)
            local_stock_before = int(
                await session.scalar(
                    select(func.count(InventoryItem.id)).where(
                        InventoryItem.product_id == product.id,
                        InventoryItem.status == "available",
                    )
                )
                or 0
            )
            supplier_stock = (
                max(0, int(product.supplier_available_stock))
                if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
                else 0
            )
            total_stock_before = local_stock_before + supplier_stock
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(candidate.raw_item),
                        account_fingerprint=candidate.account_fingerprint,
                        cost_amount=parsed_cost,
                    )
                    for candidate in duplicate_check.accepted
                ]
            )
            await session.flush()
            if (
                lock_sale_price is not None
                and product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
            ):
                product.price_lock_enabled = True
                lock_applied = True
                await session.execute(
                    update(ProductPriceAlert)
                    .where(
                        ProductPriceAlert.product_id == product.id,
                        ProductPriceAlert.status == "pending",
                    )
                    .values(status="superseded")
                )
            local_stock = int(
                await session.scalar(
                    select(func.count(InventoryItem.id)).where(
                        InventoryItem.product_id == product.id,
                        InventoryItem.status == "available",
                    )
                )
                or 0
            )
            total_stock_after = local_stock + supplier_stock
            if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
                product.external_stock = (
                    local_stock + max(0, int(product.supplier_available_stock))
                )
            notification_queued = False
            notification_note = ""
            if notify_stock_arrival is not None:
                notification_queued = await queue_inventory_stock_alert(
                    session,
                    product,
                    stock_before=total_stock_before,
                    stock_after=total_stock_after,
                )
                if notification_queued:
                    notification_note = " và đã xếp thông báo hàng về"
                elif getattr(product, "stock_notifications_enabled", True) is False:
                    notification_note = "; công tắc thông báo hàng về đang tắt"
                elif not product.active or product.force_out_of_stock:
                    notification_note = "; sản phẩm đang ẩn hoặc đang bị đưa về 0 hàng nên không gửi thông báo"
            await session.commit()
        lock_note = " và đã khóa giá bán" if lock_applied else ""
        flash(
            request,
            f"Đã thêm {len(accepted_items)} sản phẩm vào kho với giá vốn "
            f"{format_vnd(parsed_cost)}/tài khoản{lock_note}{notification_note}"
            f"; bỏ qua {duplicate_count} tài khoản nghi ngờ/trùng."
            if duplicate_count
            else f"Đã thêm {len(accepted_items)} sản phẩm vào kho với giá vốn "
            f"{format_vnd(parsed_cost)}/tài khoản{lock_note}{notification_note}.",
        )
        return RedirectResponse("/admin/inventory", status_code=303)

    @router.post("/admin/inventory/withdraw")
    async def withdraw_inventory(
        request: Request,
        csrf: str = Form(...),
        product_id: int = Form(...),
        quantity: int = Form(...),
        source: str = Form("auto"),
        reason: str = Form("Bảo hành khách hàng"),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/inventory", status_code=303)
        if quantity < 1 or quantity > MAX_INVENTORY_WITHDRAWAL_QUANTITY:
            flash(
                request,
                f"Số lượng rút phải từ 1 đến {MAX_INVENTORY_WITHDRAWAL_QUANTITY}.",
                "error",
            )
            return RedirectResponse("/admin/inventory", status_code=303)
        normalized_source = source.strip().lower()
        if normalized_source not in {"auto", "local", "api"}:
            flash(request, "Nguồn rút hàng không hợp lệ.", "error")
            return RedirectResponse("/admin/inventory", status_code=303)

        normalized_reason = reason.strip()[:500] or "Bảo hành khách hàng"
        withdrawn_by = str(request.session.get("dashboard_admin") or "admin")[:255]
        withdrawal_code = f"WD{secrets.token_hex(8).upper()}"
        async with session_factory() as lookup_session:
            product_snapshot = await lookup_session.get(Product, product_id)
        if product_snapshot is None:
            flash(request, "Sản phẩm rút kho không hợp lệ.", "error")
            return RedirectResponse("/admin/inventory", status_code=303)

        balance_clients = (
            supplier_balance_clients_for_product(
                product_snapshot,
                supplier_client,
                lehai_client,
                canboso_client,
                nce_client,
                haji_client,
            )
            if normalized_source != "local"
            else ()
        )
        async with AsyncExitStack() as supplier_locks:
            for client in balance_clients:
                await supplier_locks.enter_async_context(supplier_balance_guard(client))

            async with session_factory() as session:
                product = await session.scalar(
                    select(Product).where(Product.id == product_id).with_for_update()
                )
                if (
                    product is None
                    or product.archived_at is not None
                    or product.fulfillment_source not in SELLABLE_FULFILLMENT_SOURCES
                    or product.product_type != "account"
                ):
                    flash(request, "Sản phẩm rút kho không hợp lệ.", "error")
                    return RedirectResponse("/admin/inventory", status_code=303)

                available_count = int(
                    await session.scalar(
                        select(func.count(InventoryItem.id)).where(
                            InventoryItem.product_id == product.id,
                            InventoryItem.status == "available",
                        )
                    )
                    or 0
                )
                use_api = normalized_source == "api" or (
                    normalized_source == "auto"
                    and available_count < quantity
                    and product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
                )

                if not use_api:
                    if available_count < quantity:
                        await session.rollback()
                        flash(
                            request,
                            f"Kho nhập chỉ còn {available_count} tài khoản; không rút một "
                            "phần. Hãy giảm số lượng hoặc chọn nguồn API.",
                            "error",
                        )
                        return RedirectResponse("/admin/inventory", status_code=303)

                    items = list(
                        await session.scalars(
                            select(InventoryItem)
                            .where(
                                InventoryItem.product_id == product.id,
                                InventoryItem.status == "available",
                            )
                            .order_by(InventoryItem.id)
                            .with_for_update(skip_locked=True)
                            .limit(quantity)
                        )
                    )
                    if len(items) != quantity:
                        await session.rollback()
                        flash(
                            request,
                            "Một phần kho đang được giao cho đơn khác. Chưa rút tài khoản "
                            "nào; hãy thử lại sau vài giây.",
                            "error",
                        )
                        return RedirectResponse("/admin/inventory", status_code=303)

                    withdrawn_at = datetime.now(UTC)
                    for item in items:
                        item.status = "withdrawn"
                        item.withdrawal_code = withdrawal_code
                        item.withdrawn_at = withdrawn_at
                        item.withdrawn_by = withdrawn_by
                        item.withdrawal_reason = normalized_reason
                    await session.flush()

                    local_stock = available_count - quantity
                    if product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES:
                        product.external_stock = local_stock + max(
                            0, int(product.supplier_available_stock)
                        )
                        if product.price_lock_enabled and local_stock == 0:
                            await release_price_lock_if_inventory_empty(session, product)
                    await session.commit()
                else:
                    if (
                        product.fulfillment_source not in EXTERNAL_FULFILLMENT_SOURCES
                        or not product.supplier_product_id
                        or not enabled_supplier_providers(product)
                    ):
                        await session.rollback()
                        flash(
                            request,
                            "Sản phẩm này chưa bật nguồn API để rút bảo hành.",
                            "error",
                        )
                        return RedirectResponse("/admin/inventory", status_code=303)

                    request_key = f"warranty-{withdrawal_code.lower()}"
                    try:
                        if is_multi_supplier_product(
                            product.fulfillment_source,
                            product.supplier_product_id,
                        ):
                            route_fetch = await fetch_product_supplier_routes(
                                product.fulfillment_source,
                                product.supplier_product_id,
                                supplier_client,
                                lehai_client,
                                canboso_client,
                                nce_client,
                                haji_client,
                                enabled_providers=enabled_supplier_providers(product),
                            )
                            selected_route = next(
                                (
                                    route
                                    for route in sorted(
                                        route_fetch.routes,
                                        key=supplier_route_sort_key,
                                    )
                                    if route.snapshot.effective_stock >= quantity
                                ),
                                None,
                            )
                            if selected_route is None:
                                await session.rollback()
                                flash(
                                    request,
                                    "Không có một nguồn API riêng lẻ nào đủ số lượng để "
                                    "rút an toàn.",
                                    "error",
                                )
                                return RedirectResponse(
                                    "/admin/inventory", status_code=303
                                )
                            purchase = await buy_supplier_product(
                                session,
                                selected_route.client,
                                selected_route.product_id,
                                quantity,
                                idempotency_key=request_key,
                                shop_product_id=product.id,
                            )
                            supplier_purchases = (
                                (
                                    purchase,
                                    max(
                                        0,
                                        int(
                                            purchase.unit_price
                                            or selected_route.snapshot.unit_price
                                            or 0
                                        ),
                                    ),
                                ),
                            )
                            api_stock_before = sum(
                                route.snapshot.effective_stock
                                for route in route_fetch.routes
                            )
                        else:
                            external_client = supplier_client_for_product(
                                product,
                                supplier_client,
                                lehai_client,
                                canboso_client,
                                nce_client,
                                haji_client,
                            )
                            if external_client is None:
                                await session.rollback()
                                flash(
                                    request,
                                    "Nguồn API của sản phẩm đang tắt hoặc chưa cấu hình.",
                                    "error",
                                )
                                return RedirectResponse(
                                    "/admin/inventory", status_code=303
                                )
                            api_snapshot = await external_client.fetch_snapshot(
                                product.supplier_product_id
                            )
                            if api_snapshot.effective_stock < quantity:
                                await session.rollback()
                                flash(
                                    request,
                                    "Nguồn API hiện không đủ số lượng để rút.",
                                    "error",
                                )
                                return RedirectResponse(
                                    "/admin/inventory", status_code=303
                                )
                            purchase = await buy_supplier_product(
                                session,
                                external_client,
                                product.supplier_product_id,
                                quantity,
                                idempotency_key=request_key,
                                shop_product_id=product.id,
                            )
                            supplier_purchases = (
                                (
                                    purchase,
                                    max(
                                        0,
                                        int(
                                            purchase.unit_price
                                            or api_snapshot.unit_price
                                            or product.supplier_price
                                            or 0
                                        ),
                                    ),
                                ),
                            )
                            api_stock_before = api_snapshot.effective_stock
                    except SupplierError as exc:
                        pending_recoveries = list(
                            await session.scalars(
                                select(SupplierRecoveryRequest)
                                .where(
                                    SupplierRecoveryRequest.product_id == product.id,
                                    SupplierRecoveryRequest.status == "pending",
                                    SupplierRecoveryRequest.request_key.startswith(
                                        request_key
                                    ),
                                )
                                .with_for_update()
                            )
                        )
                        for recovery in pending_recoveries:
                            recovery.inventory_withdrawal_code = withdrawal_code
                            recovery.inventory_withdrawn_by = withdrawn_by
                            recovery.inventory_withdrawal_reason = normalized_reason
                        await session.commit()
                        logger.warning(
                            "Admin API warranty withdrawal failed: product=%s quantity=%s "
                            "code=%s",
                            product.id,
                            quantity,
                            exc.code,
                        )
                        messages = {
                            "INSUFFICIENT_STOCK": "Nguồn API hiện không đủ hàng để rút.",
                            "INSUFFICIENT_BALANCE": "Số dư nguồn API không đủ để rút hàng.",
                            "SUPPLIER_RECOVERY_PENDING": (
                                "Kết quả rút từ API đang chờ đối soát; không bấm lại để "
                                "tránh mua trùng."
                            ),
                        }
                        recovery_message = (
                            "Kết quả rút từ API đang chờ đối soát; không bấm lại để "
                            "tránh mua trùng. Nếu nguồn xác nhận đơn, tài khoản sẽ tự "
                            "xuất hiện trong lịch sử rút bảo hành."
                            if pending_recoveries
                            else None
                        )
                        flash(
                            request,
                            recovery_message
                            or messages.get(
                                exc.code,
                                "Nguồn API đang lỗi hoặc phản hồi chưa xác định. Hãy "
                                "kiểm tra đối soát trước khi thử lại.",
                            ),
                            "error",
                        )
                        return RedirectResponse("/admin/inventory", status_code=303)

                    delivered_count = sum(
                        len(purchase.accounts) for purchase, _unit_cost in supplier_purchases
                    )
                    if delivered_count != quantity:
                        await preserve_supplier_purchase_parts(
                            session,
                            product,
                            supplier_purchases,
                            cipher,
                        )
                        await session.commit()
                        flash(
                            request,
                            "API trả thiếu hàng. Phần đã nhận được giữ an toàn trong kho; "
                            "chưa tạo lần rút bảo hành.",
                            "error",
                        )
                        return RedirectResponse("/admin/inventory", status_code=303)

                    withdrawn_at = datetime.now(UTC)
                    for purchase, unit_cost in supplier_purchases:
                        for item_index, secret_value in enumerate(purchase.accounts):
                            session.add(
                                InventoryItem(
                                    product_id=product.id,
                                    encrypted_secret=cipher.encrypt(secret_value),
                                    account_fingerprint=cipher.inventory_fingerprint(
                                        secret_value
                                    ),
                                    cost_amount=unit_cost,
                                    supplier_order_code=purchase.order_code or None,
                                    supplier_provider=purchase.provider,
                                    supplier_item_index=item_index,
                                    status="withdrawn",
                                    withdrawal_code=withdrawal_code,
                                    withdrawn_at=withdrawn_at,
                                    withdrawn_by=withdrawn_by,
                                    withdrawal_reason=normalized_reason,
                                )
                            )
                        record_supplier_purchase(
                            session,
                            amount=unit_cost * len(purchase.accounts),
                            supplier_order_code=purchase.order_code or None,
                            shop_order_code=withdrawal_code,
                            product_id=product.id,
                            quantity=len(purchase.accounts),
                            provider=purchase.provider,
                        )

                    supplier_stock_after = max(0, api_stock_before - quantity)
                    product.supplier_available_stock = supplier_stock_after
                    product.external_stock = available_count + supplier_stock_after
                    product.supplier_synced_at = datetime.now(UTC)
                    if product.price_lock_enabled and available_count == 0:
                        await release_price_lock_if_inventory_empty(session, product)
                    await session.commit()

        return RedirectResponse(
            f"/admin/inventory/withdrawals/{withdrawal_code}",
            status_code=303,
        )

    @router.get("/admin/inventory/withdrawals/{withdrawal_code}", response_class=HTMLResponse)
    async def inventory_withdrawal_detail(
        withdrawal_code: str,
        request: Request,
    ) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        if not re.fullmatch(r"WD[A-F0-9]{16}", withdrawal_code):
            raise HTTPException(status_code=404, detail="Withdrawal not found")
        async with session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        select(InventoryItem, Product)
                        .join(Product, Product.id == InventoryItem.product_id)
                        .where(
                            InventoryItem.withdrawal_code == withdrawal_code,
                            InventoryItem.status == "withdrawn",
                        )
                        .order_by(InventoryItem.id)
                    )
                ).all()
            )
        if not rows:
            raise HTTPException(status_code=404, detail="Withdrawal not found")

        secrets_for_copy: list[str] = []
        item_ids: list[int] = []
        total_cost = 0
        source_summary: dict[tuple[str, str | None], dict[str, object]] = {}
        for item, _product in rows:
            item_ids.append(item.id)
            total_cost += int(item.cost_amount)
            provider = item.supplier_provider or "local"
            source_key = (provider, item.supplier_order_code)
            source_row = source_summary.setdefault(
                source_key,
                {
                    "label": SUPPLIER_PROVIDER_LABELS.get(provider, "Kho nhập"),
                    "order_code": item.supplier_order_code,
                    "quantity": 0,
                    "cost": 0,
                },
            )
            source_row["quantity"] = int(source_row["quantity"]) + 1
            source_row["cost"] = int(source_row["cost"]) + int(item.cost_amount)
            try:
                secrets_for_copy.append(cipher.decrypt(item.encrypted_secret))
            except Exception:
                secrets_for_copy.append(f"[Không thể giải mã mục kho #{item.id}]")

        first_item, product = rows[0]
        response = templates.TemplateResponse(
            request,
            "inventory_withdrawal_detail.html",
            page_context(
                request,
                f"Rút kho {withdrawal_code}",
                "inventory",
                withdrawal_code=withdrawal_code,
                product=product,
                quantity=len(rows),
                withdrawn_at=first_item.withdrawn_at,
                withdrawn_by=first_item.withdrawn_by,
                withdrawal_reason=first_item.withdrawal_reason,
                withdrawal_secrets=secrets_for_copy,
                item_ids=item_ids,
                total_cost=total_cost,
                withdrawal_sources=list(source_summary.values()),
                withdrawal_source_label=(
                    "API" if first_item.supplier_provider else "Kho nhập"
                ),
            ),
        )
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        return response

    @router.post("/admin/inventory/{item_id}/delete")
    async def delete_inventory_item(
        item_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/inventory", status_code=303)
        async with session_factory() as session:
            product_id = await session.scalar(
                select(InventoryItem.product_id).where(InventoryItem.id == item_id)
            )
            if product_id is None:
                flash(request, "Chỉ có thể xóa mục kho chưa bán.", "error")
                return RedirectResponse("/admin/inventory", status_code=303)
            product = await session.scalar(
                select(Product).where(Product.id == product_id).with_for_update()
            )
            item = await session.scalar(
                select(InventoryItem).where(InventoryItem.id == item_id).with_for_update()
            )
            if (
                item is None
                or item.product_id != product_id
                or item.status != "available"
            ):
                flash(request, "Chỉ có thể xóa mục kho chưa bán.", "error")
                return RedirectResponse("/admin/inventory", status_code=303)
            await session.delete(item)
            await session.flush()
            if (
                product is not None
                and product.fulfillment_source in EXTERNAL_FULFILLMENT_SOURCES
            ):
                local_stock = int(
                    await session.scalar(
                        select(func.count(InventoryItem.id)).where(
                            InventoryItem.product_id == product.id,
                            InventoryItem.status == "available",
                        )
                    )
                    or 0
                )
                product.external_stock = (
                    local_stock + max(0, int(product.supplier_available_stock))
                )
                if product.price_lock_enabled:
                    await release_price_lock_if_inventory_empty(session, product)
            await session.commit()
        flash(request, f"Đã xóa mục kho #{item_id}.")
        return RedirectResponse("/admin/inventory", status_code=303)

    @router.get("/admin/users", response_class=HTMLResponse)
    async def users_page(
        request: Request,
        q: str = "",
        status: str = "all",
        page: int = 1,
    ) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        user_conditions = []
        normalized_query = q.strip().lstrip("@").strip()
        if normalized_query:
            needle = f"%{normalized_query}%"
            user_conditions.append(
                or_(
                    User.username.ilike(needle),
                    User.full_name.ilike(needle),
                    cast(User.telegram_id, String).ilike(needle),
                )
            )
        async with session_factory() as session:
            order_stats = (
                select(
                    Order.user_id.label("user_id"),
                    purchase_order_count().label("order_count"),
                    func.coalesce(func.sum(Order.amount), 0).label("spent"),
                    func.max(Order.created_at).label("last_order_at"),
                )
                .group_by(Order.user_id)
                .subquery()
            )
            sms_stats = (
                select(
                    SmsRental.user_id.label("user_id"),
                    func.count(SmsRental.id).label("sms_count"),
                    func.coalesce(func.sum(SmsRental.sale_amount), 0).label("sms_spent"),
                    func.max(SmsRental.completed_at).label("last_sms_at"),
                )
                .where(SmsRental.status == "success")
                .group_by(SmsRental.user_id)
                .subquery()
            )
            deposit_stats = (
                select(
                    PaymentTransaction.user_id.label("user_id"),
                    func.coalesce(func.sum(PaymentTransaction.amount), 0).label("deposited"),
                    func.max(PaymentTransaction.created_at).label("last_deposit_at"),
                )
                .join(Deposit, Deposit.id == PaymentTransaction.deposit_id)
                .where(
                    PaymentTransaction.credit_status == "credited",
                    Deposit.payment_kind == "wallet",
                )
                .group_by(PaymentTransaction.user_id)
                .subquery()
            )
            total_spent = (
                func.coalesce(order_stats.c.spent, 0)
                + func.coalesce(sms_stats.c.sms_spent, 0)
            )
            if status == "blocked":
                user_conditions.append(User.is_blocked.is_(True))
            elif status == "started":
                user_conditions.append(User.has_started.is_(True))
            elif status == "inactive":
                user_conditions.append(User.has_started.is_(False))
            elif status == "wallet":
                user_conditions.append(User.balance > 0)
            elif status == "spent":
                user_conditions.append(total_spent > 0)
            elif status == "potential":
                user_conditions.extend(
                    (
                        User.has_started.is_(True),
                        User.is_blocked.is_(False),
                        total_spent == 0,
                    )
                )
            user_count_statement = (
                select(
                    func.count(User.telegram_id),
                    func.coalesce(func.sum(User.balance), 0),
                )
                .outerjoin(order_stats, order_stats.c.user_id == User.telegram_id)
                .outerjoin(sms_stats, sms_stats.c.user_id == User.telegram_id)
            )
            if user_conditions:
                user_count_statement = user_count_statement.where(*user_conditions)
            user_count, filtered_wallet_total = (
                await session.execute(user_count_statement)
            ).one()
            pager = admin_pager(request, int(user_count), page)
            statement = (
                select(
                    User,
                    func.coalesce(order_stats.c.order_count, 0),
                    func.coalesce(order_stats.c.spent, 0),
                    order_stats.c.last_order_at,
                    func.coalesce(sms_stats.c.sms_count, 0),
                    func.coalesce(sms_stats.c.sms_spent, 0),
                    sms_stats.c.last_sms_at,
                    func.coalesce(deposit_stats.c.deposited, 0),
                    deposit_stats.c.last_deposit_at,
                )
                .outerjoin(order_stats, order_stats.c.user_id == User.telegram_id)
                .outerjoin(sms_stats, sms_stats.c.user_id == User.telegram_id)
                .outerjoin(deposit_stats, deposit_stats.c.user_id == User.telegram_id)
                .offset(pager.offset)
                .limit(ADMIN_PAGE_SIZE)
            )
            if user_conditions:
                statement = statement.where(*user_conditions)
            if status == "potential":
                statement = statement.order_by(
                    User.balance.desc(),
                    func.coalesce(deposit_stats.c.deposited, 0).desc(),
                    User.updated_at.desc(),
                    User.created_at.desc(),
                )
            elif status == "spent":
                statement = statement.order_by(
                    total_spent.desc(),
                    order_stats.c.last_order_at.desc(),
                    sms_stats.c.last_sms_at.desc(),
                    User.created_at.desc(),
                )
            else:
                statement = statement.order_by(User.created_at.desc())
            user_rows = [
                {
                    "user": user,
                    "order_count": int(order_count),
                    "spent": int(spent) + int(sms_spent),
                    "last_order_at": last_order_at,
                    "sms_count": int(sms_count),
                    "last_sms_at": last_sms_at,
                    "deposited": int(deposited),
                    "last_deposit_at": last_deposit_at,
                }
                for (
                    user,
                    order_count,
                    spent,
                    last_order_at,
                    sms_count,
                    sms_spent,
                    last_sms_at,
                    deposited,
                    last_deposit_at,
                )
                in await session.execute(statement)
            ]
        return templates.TemplateResponse(
            request,
            "users.html",
            page_context(
                request,
                "Khách hàng",
                "users",
                users=user_rows,
                query=q,
                status=status,
                pager=pager,
                filtered_wallet_total=int(filtered_wallet_total),
            ),
        )

    @router.get("/admin/users/{user_id}", response_class=HTMLResponse)
    async def user_detail_page(
        user_id: int,
        request: Request,
        kind: str = "all",
        page: int = 1,
    ) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        selected_kind = kind if kind in WALLET_KIND_LABELS else "all"
        async with session_factory() as session:
            user = await session.get(User, user_id)
            if user is None:
                return Response("Không tìm thấy khách hàng.", status_code=404)

            ledger_conditions = [WalletTransaction.user_id == user.telegram_id]
            if selected_kind != "all":
                ledger_conditions.append(WalletTransaction.kind == selected_kind)
            transaction_count = int(
                await session.scalar(
                    select(func.count(WalletTransaction.id)).where(*ledger_conditions)
                )
                or 0
            )
            pager = admin_pager(request, transaction_count, page)
            transactions = list(
                await session.scalars(
                    select(WalletTransaction)
                    .where(*ledger_conditions)
                    .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
                    .offset(pager.offset)
                    .limit(ADMIN_PAGE_SIZE)
                )
            )
            total_credits = int(
                await session.scalar(
                    select(func.coalesce(func.sum(WalletTransaction.amount), 0)).where(
                        WalletTransaction.user_id == user.telegram_id,
                        WalletTransaction.amount > 0,
                        WalletTransaction.kind != "opening_balance",
                    )
                )
                or 0
            )
            total_debits = abs(
                int(
                    await session.scalar(
                        select(func.coalesce(func.sum(WalletTransaction.amount), 0)).where(
                            WalletTransaction.user_id == user.telegram_id,
                            WalletTransaction.amount < 0,
                        )
                    )
                    or 0
                )
            )
            opening_balance = int(
                await session.scalar(
                    select(WalletTransaction.balance_after)
                    .where(
                        WalletTransaction.user_id == user.telegram_id,
                        WalletTransaction.kind == "opening_balance",
                    )
                    .order_by(WalletTransaction.id)
                    .limit(1)
                )
                or 0
            )
            order_count = int(
                await session.scalar(
                    select(purchase_order_count()).where(Order.user_id == user.telegram_id)
                )
                or 0
            )
            sms_count = int(
                await session.scalar(
                    select(func.count(SmsRental.id)).where(
                        SmsRental.user_id == user.telegram_id
                    )
                )
                or 0
            )
        return templates.TemplateResponse(
            request,
            "user_detail.html",
            page_context(
                request,
                f"Khách hàng {user.full_name}",
                "users",
                user=user,
                transactions=transactions,
                transaction_count=transaction_count,
                total_credits=total_credits,
                total_debits=total_debits,
                opening_balance=opening_balance,
                order_count=order_count,
                sms_count=sms_count,
                selected_kind=selected_kind,
                wallet_kind_labels=WALLET_KIND_LABELS,
                wallet_reference_labels=WALLET_REFERENCE_LABELS,
                pager=pager,
            ),
        )

    @router.get("/admin/broadcasts", response_class=HTMLResponse)
    async def broadcasts_page(
        request: Request,
        tab: str = "admin",
        broadcast_page: int = 1,
        sale_page: int = 1,
        stock_page: int = 1,
    ) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        selected_tab = tab if tab in {"admin", "sale", "stock"} else "admin"
        async with session_factory() as session:
            active_recipients = int(
                await session.scalar(
                    select(func.count(User.telegram_id)).where(User.has_started.is_(True))
                )
                or 0
            )
            broadcast_count = int(
                await session.scalar(select(func.count(BroadcastLog.id))) or 0
            )
            active_broadcasts = int(
                await session.scalar(
                    select(func.count(BroadcastLog.id)).where(
                        BroadcastLog.status.in_(("queued", "sending"))
                    )
                )
                or 0
            )
            delivered_count = int(
                await session.scalar(
                    select(func.coalesce(func.sum(BroadcastLog.delivered_count), 0))
                )
                or 0
            )
            failed_count = int(
                await session.scalar(
                    select(func.coalesce(func.sum(BroadcastLog.failed_count), 0))
                )
                or 0
            )
            broadcast_pager = admin_pager(
                request,
                broadcast_count,
                broadcast_page,
                page_parameter="broadcast_page",
            )
            broadcast_records = []
            if selected_tab == "admin":
                broadcast_records = list(
                    await session.scalars(
                        select(BroadcastLog)
                        .order_by(BroadcastLog.id.desc())
                        .offset(broadcast_pager.offset)
                        .limit(ADMIN_PAGE_SIZE)
                    )
                )
            broadcast_ids = [broadcast.id for broadcast in broadcast_records]
            failure_groups: dict[int, list[dict[str, object]]] = {}
            if broadcast_ids:
                for broadcast_id, error, count in await session.execute(
                    select(
                        BroadcastDelivery.broadcast_id,
                        BroadcastDelivery.last_error,
                        func.count(BroadcastDelivery.id),
                    )
                    .where(
                        BroadcastDelivery.broadcast_id.in_(broadcast_ids),
                        BroadcastDelivery.status == "failed",
                    )
                    .group_by(
                        BroadcastDelivery.broadcast_id,
                        BroadcastDelivery.last_error,
                    )
                    .order_by(BroadcastDelivery.broadcast_id.desc())
                ):
                    failure_groups.setdefault(int(broadcast_id), []).append(
                        {
                            "error": error or "Không rõ lỗi",
                            "count": int(count),
                        }
                    )
            now = datetime.now(UTC)
            broadcasts = []
            for broadcast in broadcast_records:
                started_at = broadcast.started_at
                completed_at = broadcast.completed_at
                if started_at is not None and started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
                if completed_at is not None and completed_at.tzinfo is None:
                    completed_at = completed_at.replace(tzinfo=UTC)
                processed = broadcast.delivered_count + broadcast.failed_count
                elapsed_seconds = 0
                if started_at is not None:
                    elapsed_seconds = max(
                        0,
                        int(((completed_at or now) - started_at).total_seconds()),
                    )
                if elapsed_seconds >= 60:
                    minutes, seconds = divmod(elapsed_seconds, 60)
                    duration = f"{minutes}p {seconds}s"
                elif started_at is not None:
                    duration = f"{elapsed_seconds}s"
                else:
                    duration = "—"
                broadcasts.append(
                    {
                        "broadcast": broadcast,
                        "processed": processed,
                        "remaining": max(0, broadcast.total_recipients - processed),
                        "speed": (
                            round(processed / elapsed_seconds, 1)
                            if elapsed_seconds > 0
                            else 0
                        ),
                        "duration": duration,
                        "failures": failure_groups.get(broadcast.id, []),
                    }
                )
            sale_alert_count = int(
                await session.scalar(select(func.count(ProductPriceAlert.id))) or 0
            )
            sale_pager = admin_pager(
                request,
                sale_alert_count,
                sale_page,
                page_parameter="sale_page",
            )
            sale_records = []
            if selected_tab == "sale":
                sale_records = (
                    await session.execute(
                        select(ProductPriceAlert, Product)
                        .join(Product, Product.id == ProductPriceAlert.product_id)
                        .order_by(ProductPriceAlert.id.desc())
                        .offset(sale_pager.offset)
                        .limit(ADMIN_PAGE_SIZE)
                    )
                ).all()
            stock_alert_count = int(
                await session.scalar(select(func.count(ProductStockAlert.id))) or 0
            )
            stock_pager = admin_pager(
                request,
                stock_alert_count,
                stock_page,
                page_parameter="stock_page",
            )
            stock_records = []
            if selected_tab == "stock":
                stock_records = (
                    await session.execute(
                        select(ProductStockAlert, Product)
                        .join(Product, Product.id == ProductStockAlert.product_id)
                        .order_by(ProductStockAlert.id.desc())
                        .offset(stock_pager.offset)
                        .limit(ADMIN_PAGE_SIZE)
                    )
                ).all()
            alert_failures: dict[tuple[str, int], list[dict[str, object]]] = {}
            sale_ids = [alert.id for alert, _product in sale_records]
            stock_ids = [alert.id for alert, _product in stock_records]
            alert_filters = []
            if sale_ids:
                alert_filters.append(
                    (ProductAlertDelivery.alert_type == "sale")
                    & ProductAlertDelivery.alert_id.in_(sale_ids)
                )
            if stock_ids:
                alert_filters.append(
                    (ProductAlertDelivery.alert_type == "stock")
                    & ProductAlertDelivery.alert_id.in_(stock_ids)
                )
            if alert_filters:
                for alert_type, alert_id, error, count in await session.execute(
                    select(
                        ProductAlertDelivery.alert_type,
                        ProductAlertDelivery.alert_id,
                        ProductAlertDelivery.last_error,
                        func.count(ProductAlertDelivery.id),
                    )
                    .where(
                        or_(*alert_filters),
                        ProductAlertDelivery.status == "failed",
                    )
                    .group_by(
                        ProductAlertDelivery.alert_type,
                        ProductAlertDelivery.alert_id,
                        ProductAlertDelivery.last_error,
                    )
                ):
                    alert_failures.setdefault((str(alert_type), int(alert_id)), []).append(
                        {
                            "error": error or "Không rõ lỗi",
                            "count": int(count),
                        }
                    )

            def alert_row(alert, product, alert_type: str) -> dict[str, object]:
                started_at = alert.started_at
                completed_at = alert.completed_at
                if started_at is not None and started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=UTC)
                if completed_at is not None and completed_at.tzinfo is None:
                    completed_at = completed_at.replace(tzinfo=UTC)
                processed = alert.delivered_count + alert.failed_count
                elapsed_seconds = 0
                if started_at is not None:
                    elapsed_seconds = max(
                        0,
                        int(((completed_at or now) - started_at).total_seconds()),
                    )
                if elapsed_seconds >= 60:
                    minutes, seconds = divmod(elapsed_seconds, 60)
                    duration = f"{minutes}p {seconds}s"
                elif started_at is not None:
                    duration = f"{elapsed_seconds}s"
                else:
                    duration = "—"
                return {
                    "alert": alert,
                    "product": product,
                    "processed": processed,
                    "remaining": max(0, alert.total_recipients - processed),
                    "speed": (
                        round(processed / elapsed_seconds, 1)
                        if elapsed_seconds > 0
                        else 0
                    ),
                    "duration": duration,
                    "failures": alert_failures.get((alert_type, alert.id), []),
                }

            sale_alerts = [
                alert_row(alert, product, "sale") for alert, product in sale_records
            ]
            stock_alerts = [
                alert_row(alert, product, "stock") for alert, product in stock_records
            ]
            active_sale_alerts = int(
                await session.scalar(
                    select(func.count(ProductPriceAlert.id)).where(
                        ProductPriceAlert.status.in_(("pending", "sending"))
                    )
                )
                or 0
            )
            active_stock_alerts = int(
                await session.scalar(
                    select(func.count(ProductStockAlert.id)).where(
                        ProductStockAlert.status.in_(("pending", "sending"))
                    )
                )
                or 0
            )
            active_product_alerts = active_sale_alerts + active_stock_alerts
        return templates.TemplateResponse(
            request,
            "broadcasts.html",
            page_context(
                request,
                "Thông báo",
                "broadcasts",
                active_recipients=active_recipients,
                active_broadcasts=active_broadcasts,
                broadcast_count=broadcast_count,
                delivered_count=delivered_count,
                failed_count=failed_count,
                selected_tab=selected_tab,
                broadcasts=broadcasts,
                broadcast_pager=broadcast_pager,
                sale_alert_count=sale_alert_count,
                sale_alerts=sale_alerts,
                sale_pager=sale_pager,
                stock_alert_count=stock_alert_count,
                stock_alerts=stock_alerts,
                stock_pager=stock_pager,
                auto_refresh=active_broadcasts > 0 or active_product_alerts > 0,
            ),
        )

    @router.post("/admin/users/{user_id}/balance")
    async def adjust_balance(
        user_id: int,
        request: Request,
        csrf: str = Form(...),
        amount: str = Form(...),
        reason: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/users", status_code=303)
        sign = -1 if amount.strip().startswith("-") else 1
        parsed_amount = parse_vnd(amount)
        adjustment = sign * (parsed_amount or 0)
        clean_reason = reason.strip()
        notification_text = ""
        async with session_factory() as session:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.telegram_id == user_id).with_for_update()
                )
                if (
                    user is None
                    or adjustment == 0
                    or not clean_reason
                    or user.balance + adjustment < 0
                ):
                    flash(request, "Không thể điều chỉnh số dư.", "error")
                    return RedirectResponse("/admin/users", status_code=303)
                admin_username = str(request.session["dashboard_admin"])
                balance_adjustment = BalanceAdjustment(
                    user_id=user.telegram_id,
                    admin_username=admin_username,
                    amount=adjustment,
                    reason=clean_reason,
                )
                session.add(balance_adjustment)
                await session.flush()
                apply_wallet_change(
                    session,
                    user,
                    adjustment,
                    kind="admin_adjustment",
                    event_key=f"admin_adjustment:{balance_adjustment.id}",
                    reference_type="balance_adjustment",
                    reference_id=str(balance_adjustment.id),
                    description=f"{clean_reason} · thực hiện bởi {admin_username}",
                )
                notification_text = balance_adjustment_notification(
                    amount=adjustment,
                    balance=user.balance,
                    reason=clean_reason,
                    language=user.language,
                )
        notified = False
        if bot is not None:
            try:
                await bot.send_message(user_id, notification_text)
                notified = True
            except Exception:
                logger.exception(
                    "Could not notify user %s about Admin balance adjustment",
                    user_id,
                )
        if notified:
            flash(request, "Đã cập nhật số dư, ghi lịch sử và báo cho khách hàng.")
        else:
            flash(
                request,
                "Đã cập nhật số dư nhưng chưa gửi được thông báo Telegram cho khách.",
                "error",
            )
        return RedirectResponse(f"/admin/users/{user_id}", status_code=303)

    @router.post("/admin/users/{user_id}/toggle-block")
    async def toggle_user_block(
        user_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if valid_csrf(request, csrf):
            async with session_factory() as session:
                user = await session.get(User, user_id)
                if user is not None:
                    user.is_blocked = not user.is_blocked
                    await session.commit()
                    flash(request, "Đã cập nhật trạng thái khách hàng.")
        return RedirectResponse("/admin/users", status_code=303)

    @router.get("/admin/orders", response_class=HTMLResponse)
    async def orders_page(
        request: Request,
        q: str = "",
        status: str = "all",
        source: str = "all",
        channel: str = "all",
        period: str = "all",
        page: int = 1,
    ) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        conditions = [
            Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
            Product.product_type == "account",
        ]
        search_condition = None
        periods = dashboard_periods()
        normalized_query = q.strip()
        if normalized_query:
            needle = f"%{normalized_query}%"
            username_needle = f"%{normalized_query.lstrip('@').strip()}%"
            search_condition = or_(
                cast(Order.id, String).ilike(needle),
                Order.batch_code.ilike(needle),
                Order.supplier_order_code.ilike(needle),
                cast(User.telegram_id, String).ilike(needle),
                User.full_name.ilike(needle),
                User.username.ilike(username_needle),
                Order.product_name_vi.ilike(needle),
                Product.name_vi.ilike(needle),
            )
        if status in {"completed", "pending", "failed"}:
            conditions.append(Order.status == status)
        if source in SELLABLE_FULFILLMENT_SOURCES:
            conditions.append(order_supplier_provider_expression() == source)
        if channel in {"telegram", "api"}:
            conditions.append(Order.sales_channel == channel)
        if period == "today":
            conditions.append(Order.created_at >= periods["today"])
        elif period == "month":
            conditions.append(Order.created_at >= periods["month"])
        elif period == "year":
            conditions.append(Order.created_at >= periods["year"])
        async with session_factory() as session:
            matching_keys = None
            if search_condition is not None:
                matching_statement = (
                    select(order_group_key().label("group_key"))
                    .select_from(Order)
                    .join(Product, Product.id == Order.product_id)
                    .join(User, User.telegram_id == Order.user_id)
                    .where(*conditions, search_condition)
                    .distinct()
                )
                matching_keys = matching_statement.subquery()
            summary_statement = (
                select(
                    purchase_order_count(),
                    func.coalesce(func.sum(Order.amount), 0),
                    func.coalesce(func.sum(Order.cost_amount), 0),
                    func.coalesce(func.sum(Order.discount_amount), 0),
                    func.count(func.distinct(Order.user_id)),
                )
                .select_from(Order)
                .join(Product, Product.id == Order.product_id)
                .join(User, User.telegram_id == Order.user_id)
            )
            if conditions:
                summary_statement = summary_statement.where(*conditions)
            if matching_keys is not None:
                summary_statement = summary_statement.where(
                    order_group_key().in_(select(matching_keys.c.group_key))
                )
            order_count, revenue, cost, discount, customer_count = (
                await session.execute(summary_statement)
            ).one()
            pager = admin_pager(request, int(order_count), page)
            paged_group_key = order_group_key()
            paged_keys_statement = (
                select(
                    paged_group_key.label("group_key"),
                    func.max(Order.id).label("latest_order_id"),
                )
                .select_from(Order)
                .join(Product, Product.id == Order.product_id)
                .join(User, User.telegram_id == Order.user_id)
                .where(*conditions)
                .group_by(paged_group_key)
                .order_by(func.max(Order.id).desc())
                .offset(pager.offset)
                .limit(ADMIN_PAGE_SIZE)
            )
            if matching_keys is not None:
                paged_keys_statement = paged_keys_statement.where(
                    paged_group_key.in_(select(matching_keys.c.group_key))
                )
            paged_keys = paged_keys_statement.subquery()
            rows = await session.execute(
                select(Order, Product, User)
                .join(Product, Product.id == Order.product_id)
                .join(User, User.telegram_id == Order.user_id)
                .where(order_group_key().in_(select(paged_keys.c.group_key)))
                .order_by(Order.id.desc())
            )
            orders = group_order_rows(rows)
            reward_keys = (
                select(order_group_key())
                .select_from(Order)
                .join(Product, Product.id == Order.product_id)
                .join(User, User.telegram_id == Order.user_id)
                .where(*conditions)
                .distinct()
            )
            if matching_keys is not None:
                reward_keys = reward_keys.where(
                    order_group_key().in_(select(matching_keys.c.group_key))
                )
            referral = int(
                await session.scalar(
                    select(func.coalesce(func.sum(ReferralReward.commission_amount), 0)).where(
                        ReferralReward.shop_order_code.in_(reward_keys)
                    )
                )
                or 0
            )
        return templates.TemplateResponse(
            request,
            "orders.html",
            page_context(
                request,
                "Đơn hàng",
                "orders",
                orders=orders,
                query=q,
                status=status,
                source=source,
                channel=channel,
                period=period,
                pager=pager,
                summary={
                    "orders": int(order_count),
                    "revenue": int(revenue),
                    "cost": int(cost),
                    "referral": referral,
                    "profit": int(revenue) - int(cost) - referral,
                    "discount": int(discount),
                    "customers": int(customer_count),
                },
            ),
        )

    @router.get("/admin/orders/{order_id}", response_class=HTMLResponse)
    async def order_detail_page(order_id: int, request: Request) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(Order, Product, User)
                    .join(Product, Product.id == Order.product_id)
                    .join(User, User.telegram_id == Order.user_id)
                    .where(
                        Order.id == order_id,
                        Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                        Product.product_type == "account",
                    )
                )
            ).one_or_none()
            if row is None:
                return RedirectResponse("/admin/orders", status_code=303)
            order, product, user = row
            related_statement = (
                select(Order, InventoryItem)
                .join(InventoryItem, InventoryItem.id == Order.inventory_item_id)
                .where(Order.user_id == user.telegram_id)
                .order_by(Order.id)
            )
            if order.batch_code:
                related_statement = related_statement.where(Order.batch_code == order.batch_code)
            else:
                related_statement = related_statement.where(Order.id == order.id)
            related_rows = list((await session.execute(related_statement)).all())
            related_orders = [related_order for related_order, _item in related_rows]
            order_group = group_order_rows(
                [(related_order, product, user) for related_order in related_orders]
            )[0]
            secret = "\n\n".join(
                f"{index}. {cipher.decrypt(item.encrypted_secret)}"
                for index, (_related_order, item) in enumerate(related_rows, start=1)
            )
            user_order_count = int(
                await session.scalar(
                    select(purchase_order_count())
                    .join(Product, Product.id == Order.product_id)
                    .where(
                        Order.user_id == user.telegram_id,
                        Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                        Product.product_type == "account",
                    )
                )
                or 0
            )
            user_spent = int(
                await session.scalar(
                    select(func.coalesce(func.sum(Order.amount), 0))
                    .join(Product, Product.id == Order.product_id)
                    .where(
                        Order.user_id == user.telegram_id,
                        Product.fulfillment_source.in_(SELLABLE_FULFILLMENT_SOURCES),
                        Product.product_type == "account",
                    )
                )
                or 0
            )
            deposits = list(
                await session.scalars(
                    select(Deposit)
                    .where(Deposit.user_id == user.telegram_id)
                    .order_by(Deposit.id.desc())
                    .limit(8)
                )
            )
            adjustments = list(
                await session.scalars(
                    select(BalanceAdjustment)
                    .where(BalanceAdjustment.user_id == user.telegram_id)
                    .order_by(BalanceAdjustment.id.desc())
                    .limit(8)
                )
            )
            referral_reward = await session.scalar(
                select(ReferralReward).where(
                    ReferralReward.shop_order_code == str(order_group["shop_order_code"])
                )
            )
        return templates.TemplateResponse(
            request,
            "order_detail.html",
            page_context(
                request,
                f"Đơn hàng {order_group['shop_order_code']}",
                "orders",
                order=order_group,
                product=product,
                user=user,
                secret=secret,
                related_orders=related_orders,
                user_order_count=user_order_count,
                user_spent=user_spent,
                deposits=deposits,
                adjustments=adjustments,
                referral_reward=referral_reward,
            ),
        )

    @router.get("/admin/api-clients", response_class=HTMLResponse)
    async def api_clients_page(
        request: Request,
        q: str = "",
        status: str = "all",
        page: int = 1,
    ) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        selected_status = (
            status
            if status in {"all", "active", "paused", "blocked", "attention"}
            else "all"
        )
        recent_since = datetime.now(UTC) - timedelta(hours=24)
        async with session_factory() as session:
            order_stats = (
                select(
                    Order.api_client_id.label("api_client_id"),
                    purchase_order_count().label("order_count"),
                    func.coalesce(func.sum(Order.amount), 0).label("revenue"),
                )
                .where(Order.api_client_id.is_not(None))
                .group_by(Order.api_client_id)
                .subquery()
            )
            request_stats = (
                select(
                    ApiRequestAudit.api_client_id.label("api_client_id"),
                    func.count(ApiRequestAudit.id).label("request_count"),
                    func.count(ApiRequestAudit.id)
                    .filter(ApiRequestAudit.created_at >= recent_since)
                    .label("recent_request_count"),
                    func.count(ApiRequestAudit.id)
                    .filter(
                        ApiRequestAudit.created_at >= recent_since,
                        ApiRequestAudit.status_code >= 400,
                    )
                    .label("recent_error_count"),
                    func.coalesce(
                        func.avg(ApiRequestAudit.duration_ms).filter(
                            ApiRequestAudit.created_at >= recent_since
                        ),
                        0,
                    ).label("average_duration_ms"),
                    func.max(ApiRequestAudit.created_at).label("last_request_at"),
                )
                .where(ApiRequestAudit.api_client_id.is_not(None))
                .group_by(ApiRequestAudit.api_client_id)
                .subquery()
            )
            statement = (
                select(
                    ApiClient,
                    User,
                    func.coalesce(order_stats.c.order_count, 0),
                    func.coalesce(order_stats.c.revenue, 0),
                    func.coalesce(request_stats.c.request_count, 0),
                    func.coalesce(request_stats.c.recent_request_count, 0),
                    func.coalesce(request_stats.c.recent_error_count, 0),
                    func.coalesce(request_stats.c.average_duration_ms, 0),
                    request_stats.c.last_request_at,
                )
                .join(User, User.telegram_id == ApiClient.owner_user_id)
                .outerjoin(order_stats, order_stats.c.api_client_id == ApiClient.id)
                .outerjoin(request_stats, request_stats.c.api_client_id == ApiClient.id)
            )
            if q.strip():
                needle = f"%{q.strip()}%"
                statement = statement.where(
                    or_(
                        User.full_name.ilike(needle),
                        User.username.ilike(needle),
                        cast(User.telegram_id, String).ilike(needle),
                        ApiClient.api_id.ilike(needle),
                    )
                )
            if selected_status == "active":
                statement = statement.where(
                    ApiClient.active.is_(True),
                    ApiClient.admin_blocked.is_(False),
                )
            elif selected_status == "paused":
                statement = statement.where(
                    ApiClient.active.is_(False),
                    ApiClient.admin_blocked.is_(False),
                )
            elif selected_status == "blocked":
                statement = statement.where(ApiClient.admin_blocked.is_(True))
            elif selected_status == "attention":
                statement = statement.where(
                    or_(
                        ApiClient.admin_blocked.is_(True),
                        ApiClient.active.is_(False),
                        func.coalesce(request_stats.c.recent_error_count, 0) > 0,
                    )
                )
            client_count = int(
                await session.scalar(
                    select(func.count()).select_from(statement.subquery())
                )
                or 0
            )
            pager = admin_pager(request, client_count, page)
            statement = (
                statement
                .order_by(request_stats.c.last_request_at.desc(), ApiClient.id.desc())
                .offset(pager.offset)
                .limit(ADMIN_PAGE_SIZE)
            )
            rows = [
                {
                    "client": client,
                    "user": user,
                    "order_count": int(order_count),
                    "revenue": int(revenue),
                    "request_count": int(request_count),
                    "recent_request_count": int(recent_request_count),
                    "recent_error_count": int(recent_error_count),
                    "average_duration_ms": int(average_duration_ms),
                    "last_request_at": last_request_at,
                    "success_rate": (
                        round((recent_request_count - recent_error_count) / recent_request_count * 100, 1)
                        if recent_request_count
                        else None
                    ),
                    "needs_attention": bool(
                        client.admin_blocked
                        or not client.active
                        or recent_error_count
                    ),
                }
                for (
                    client,
                    user,
                    order_count,
                    revenue,
                    request_count,
                    recent_request_count,
                    recent_error_count,
                    average_duration_ms,
                    last_request_at,
                ) in await session.execute(statement)
            ]
            client_totals = (
                await session.execute(
                    select(
                        func.count(ApiClient.id),
                        func.count(ApiClient.id).filter(
                            ApiClient.active.is_(True),
                            ApiClient.admin_blocked.is_(False),
                        ),
                        func.count(ApiClient.id).filter(
                            ApiClient.active.is_(False),
                            ApiClient.admin_blocked.is_(False),
                        ),
                        func.count(ApiClient.id).filter(ApiClient.admin_blocked.is_(True)),
                    )
                )
            ).one()
            request_totals = (
                await session.execute(
                    select(
                        func.count(ApiRequestAudit.id),
                        func.count(ApiRequestAudit.id).filter(
                            ApiRequestAudit.status_code >= 400
                        ),
                        func.coalesce(func.avg(ApiRequestAudit.duration_ms), 0),
                    ).where(ApiRequestAudit.created_at >= recent_since)
                )
            ).one()
            stats = {
                "clients": int(client_totals[0]),
                "active": int(client_totals[1]),
                "paused": int(client_totals[2]),
                "blocked": int(client_totals[3]),
                "requests_24h": int(request_totals[0]),
                "errors_24h": int(request_totals[1]),
                "average_duration_ms": int(request_totals[2]),
                "api_orders": int(
                    await session.scalar(
                        select(func.count(func.distinct(Order.batch_code))).where(
                            Order.sales_channel == "api"
                        )
                    )
                    or 0
                ),
                "api_revenue": int(
                    await session.scalar(
                        select(func.coalesce(func.sum(Order.amount), 0)).where(
                            Order.sales_channel == "api"
                        )
                    )
                    or 0
                ),
            }
        return templates.TemplateResponse(
            request,
            "api_clients.html",
            page_context(
                request,
                "API đấu kho",
                "api-clients",
                clients=rows,
                stats=stats,
                api_base_url=settings.shop_api_base_url,
                api_audit_retention_days=settings.shop_api_audit_retention_days,
                query=q,
                status=selected_status,
                pager=pager,
            ),
        )

    @router.post("/admin/api-clients/{client_id}")
    async def update_api_client(
        client_id: int,
        request: Request,
        csrf: str = Form(...),
        rate_limit_per_minute: int = Form(60),
        allowed_ips: str = Form(""),
        admin_blocked: str | None = Form(None),
        return_q: str = Form(""),
        return_status: str = Form("all"),
        return_page: int = Form(1),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/api-clients", status_code=303)
        selected_status = (
            return_status
            if return_status in {"all", "active", "paused", "blocked", "attention"}
            else "all"
        )
        query_string = urlencode(
            {
                "q": return_q.strip(),
                "status": selected_status,
                "page": max(1, return_page),
            }
        )
        try:
            normalized_allowed_ips = normalize_allowed_ips(allowed_ips)
        except ValueError as exc:
            flash(request, f"Không thể lưu API client: {exc}")
            return RedirectResponse(f"/admin/api-clients?{query_string}", status_code=303)
        async with session_factory() as session:
            client = await session.get(ApiClient, client_id)
            if client is not None:
                client.rate_limit_per_minute = max(1, min(rate_limit_per_minute, 10_000))
                client.allowed_ips = normalized_allowed_ips
                client.admin_blocked = admin_blocked is not None
                await session.commit()
                flash(request, "Đã cập nhật API client.")
        return RedirectResponse(f"/admin/api-clients?{query_string}", status_code=303)

    @router.get("/admin/api-orders")
    async def api_orders_redirect(request: Request) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        return RedirectResponse("/admin/orders?channel=api", status_code=303)

    @router.get("/admin/referrals", response_class=HTMLResponse)
    async def referrals_page(request: Request, page: int = 1) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        referrer = aliased(User)
        referred = aliased(User)
        async with session_factory() as session:
            reward_count = int(
                await session.scalar(select(func.count(ReferralReward.id))) or 0
            )
            pager = admin_pager(request, reward_count, page)
            rewards = [
                {"reward": reward, "referrer": source, "referred": target}
                for reward, source, target in await session.execute(
                    select(ReferralReward, referrer, referred)
                    .join(referrer, referrer.telegram_id == ReferralReward.referrer_user_id)
                    .join(referred, referred.telegram_id == ReferralReward.referred_user_id)
                    .order_by(ReferralReward.id.desc())
                    .offset(pager.offset)
                    .limit(ADMIN_PAGE_SIZE)
                )
            ]
            top_referrers = [
                {"user": user, "orders": int(order_count), "commission": int(commission)}
                for user, order_count, commission in await session.execute(
                    select(
                        User,
                        func.count(ReferralReward.id),
                        func.coalesce(func.sum(ReferralReward.commission_amount), 0),
                    )
                    .join(ReferralReward, ReferralReward.referrer_user_id == User.telegram_id)
                    .group_by(User.telegram_id)
                    .order_by(func.sum(ReferralReward.commission_amount).desc())
                    .limit(20)
                )
            ]
            stats = {
                "referred_users": int(
                    await session.scalar(
                        select(func.count(User.telegram_id)).where(User.referred_by_id.is_not(None))
                    )
                    or 0
                ),
                "rewarded_orders": int(
                    await session.scalar(select(func.count(ReferralReward.id))) or 0
                ),
                "commission": int(
                    await session.scalar(
                        select(func.coalesce(func.sum(ReferralReward.commission_amount), 0))
                    )
                    or 0
                ),
                "revenue": int(
                    await session.scalar(
                        select(func.coalesce(func.sum(ReferralReward.order_amount), 0))
                    )
                    or 0
                ),
            }
        return templates.TemplateResponse(
            request,
            "referrals.html",
            page_context(
                request,
                "Giới thiệu bạn bè",
                "referrals",
                rewards=rewards,
                top_referrers=top_referrers,
                stats=stats,
                commission_percent=settings.referral_commission_percent,
                pager=pager,
            ),
        )

    @router.get("/admin/payments", response_class=HTMLResponse)
    async def payments_page(
        request: Request,
        q: str = "",
        status: str = "all",
        deposit_page: int = 1,
        transaction_page: int = 1,
        adjustment_page: int = 1,
    ) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        deposit_conditions = []
        if q.strip():
            needle = f"%{q.strip()}%"
            deposit_conditions.append(
                or_(
                    Deposit.code.ilike(needle),
                    cast(Deposit.user_id, String).ilike(needle),
                    User.full_name.ilike(needle),
                    User.username.ilike(needle),
                )
            )
        if status == "expired":
            deposit_conditions.extend(
                (
                    Deposit.status == "failed",
                    Deposit.failure_reason == "expired",
                )
            )
        elif status in {"pending", "paid", "failed"}:
            deposit_conditions.append(Deposit.status == status)
        periods = dashboard_periods()
        async with session_factory() as session:
            deposit_count_statement = (
                select(func.count(Deposit.id))
                .join(User, User.telegram_id == Deposit.user_id)
            )
            if deposit_conditions:
                deposit_count_statement = deposit_count_statement.where(
                    *deposit_conditions
                )
            deposit_count = int(
                await session.scalar(deposit_count_statement) or 0
            )
            transaction_count = int(
                await session.scalar(select(func.count(PaymentTransaction.id))) or 0
            )
            adjustment_count = int(
                await session.scalar(select(func.count(BalanceAdjustment.id))) or 0
            )
            deposit_pager = admin_pager(
                request,
                deposit_count,
                deposit_page,
                page_parameter="deposit_page",
            )
            transaction_pager = admin_pager(
                request,
                transaction_count,
                transaction_page,
                page_parameter="transaction_page",
            )
            adjustment_pager = admin_pager(
                request,
                adjustment_count,
                adjustment_page,
                page_parameter="adjustment_page",
            )
            deposit_statement = (
                select(Deposit, User)
                .join(User, User.telegram_id == Deposit.user_id)
                .order_by(Deposit.id.desc())
                .offset(deposit_pager.offset)
                .limit(ADMIN_PAGE_SIZE)
            )
            if deposit_conditions:
                deposit_statement = deposit_statement.where(*deposit_conditions)
            deposits = [
                {"deposit": deposit, "user": user}
                for deposit, user in await session.execute(deposit_statement)
            ]
            transactions = [
                {"transaction": transaction, "user": user, "deposit": deposit}
                for transaction, user, deposit in await session.execute(
                    select(PaymentTransaction, User, Deposit)
                    .join(User, User.telegram_id == PaymentTransaction.user_id)
                    .join(Deposit, Deposit.id == PaymentTransaction.deposit_id)
                    .order_by(PaymentTransaction.id.desc())
                    .offset(transaction_pager.offset)
                    .limit(ADMIN_PAGE_SIZE)
                )
            ]
            adjustments = [
                {"adjustment": adjustment, "user": user}
                for adjustment, user in await session.execute(
                    select(BalanceAdjustment, User)
                    .join(User, User.telegram_id == BalanceAdjustment.user_id)
                    .order_by(BalanceAdjustment.id.desc())
                    .offset(adjustment_pager.offset)
                    .limit(ADMIN_PAGE_SIZE)
                )
            ]
            received_total = int(
                await session.scalar(
                    select(func.coalesce(func.sum(PaymentTransaction.amount), 0)).where(
                        PaymentTransaction.credit_status == "credited"
                    )
                )
                or 0
            )
            received_today = int(
                await session.scalar(
                    select(func.coalesce(func.sum(PaymentTransaction.amount), 0)).where(
                        PaymentTransaction.created_at >= periods["today"],
                        PaymentTransaction.credit_status == "credited",
                    )
                )
                or 0
            )
            review_amount = int(
                await session.scalar(
                    select(func.coalesce(func.sum(PaymentTransaction.amount), 0)).where(
                        PaymentTransaction.credit_status.notin_(
                            ("credited", "manual_matched", "expired")
                        )
                    )
                )
                or 0
            )
            review_count = int(
                await session.scalar(
                    select(func.count(PaymentTransaction.id)).where(
                        PaymentTransaction.credit_status.notin_(
                            ("credited", "manual_matched", "expired")
                        )
                    )
                )
                or 0
            )
            pending_count = int(
                await session.scalar(
                    select(func.count(Deposit.id)).where(Deposit.status == "pending")
                )
                or 0
            )
            pending_amount = int(
                await session.scalar(
                    select(func.coalesce(func.sum(Deposit.requested_amount), 0)).where(
                        Deposit.status == "pending"
                    )
                )
                or 0
            )
        return templates.TemplateResponse(
            request,
            "payments.html",
            page_context(
                request,
                "Dòng tiền",
                "payments",
                deposits=deposits,
                transactions=transactions,
                adjustments=adjustments,
                query=q,
                status=status,
                deposit_pager=deposit_pager,
                transaction_pager=transaction_pager,
                adjustment_pager=adjustment_pager,
                stats={
                    "received_total": received_total,
                    "received_today": received_today,
                    "pending_count": pending_count,
                    "pending_amount": pending_amount,
                    "review_count": review_count,
                    "review_amount": review_amount,
                },
            ),
        )

    @router.post("/admin/payments/deposits/{deposit_id}/approve")
    async def approve_deposit_payment(
        deposit_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> Response:
        wants_json = wants_dashboard_json(request)
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            if wants_json:
                return JSONResponse(
                    {"ok": False, "status": "invalid_csrf", "message": "Phiên duyệt nạp không hợp lệ."},
                    status_code=400,
                )
            flash(request, "Phiên duyệt nạp không hợp lệ.", "error")
            return RedirectResponse("/admin/payments", status_code=303)

        result = await approve_wallet_deposit(
            session_factory,
            deposit_id,
            admin_username=str(request.session["dashboard_admin"]),
        )
        if result.status != "approved":
            messages = {
                "not_found": "Không tìm thấy yêu cầu nạp.",
                "invalid_kind": "Chỉ có thể duyệt thủ công yêu cầu nạp vào ví.",
                "already_paid": "Yêu cầu này đã được thanh toán trước đó.",
                "already_credited": "Tiền của yêu cầu này đã được cộng trước đó.",
                "invalid_status": "Trạng thái yêu cầu không thể duyệt.",
                "user_not_found": "Không tìm thấy khách hàng của yêu cầu nạp.",
            }
            message = messages.get(result.status, "Không thể duyệt tiền vào ví.")
            if wants_json:
                return JSONResponse(
                    {"ok": False, "status": result.status, "message": message},
                    status_code=409,
                )
            flash(request, message, "error")
            return RedirectResponse("/admin/payments", status_code=303)

        message = (
            f"Đã duyệt {format_vnd(result.amount)} vào ví mã {result.deposit_code}. "
            f"Số dư mới {format_vnd(result.balance)}."
        )
        if not wants_json:
            flash(request, message)
        if bot is not None and result.user_id is not None:
            try:
                await bot.send_message(
                    result.user_id,
                    "✅ <b>Khoản nạp đã được Admin duyệt</b>\n\n"
                    f"• Mã nạp: <code>{escape(result.deposit_code)}</code>\n"
                    f"• Đã cộng vào ví: <b>{format_vnd(result.amount)}</b>\n"
                    f"• Số dư hiện tại: <b>{format_vnd(result.balance)}</b>\n\n"
                    "Bạn có thể mua hàng ngay.",
                )
            except Exception:
                logger.exception(
                    "Could not notify user %s about manual deposit approval",
                    result.user_id,
                )
        if wants_json:
            return JSONResponse(
                {
                    "ok": True,
                    "status": "approved",
                    "deposit_id": deposit_id,
                    "message": message,
                    "balance": result.balance,
                }
            )
        return RedirectResponse("/admin/payments", status_code=303)

    @router.post("/admin/payments/deposits/{deposit_id}/cancel")
    async def cancel_deposit_payment(
        deposit_id: int,
        request: Request,
        csrf: str = Form(...),
    ) -> Response:
        wants_json = wants_dashboard_json(request)
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            if wants_json:
                return JSONResponse(
                    {"ok": False, "status": "invalid_csrf", "message": "Phiên hủy yêu cầu nạp không hợp lệ."},
                    status_code=400,
                )
            flash(request, "Phiên hủy yêu cầu nạp không hợp lệ.", "error")
            return RedirectResponse("/admin/payments", status_code=303)

        result = await cancel_wallet_deposit(session_factory, deposit_id)
        if result.status != "cancelled":
            messages = {
                "not_found": "Không tìm thấy yêu cầu nạp.",
                "invalid_kind": "Chỉ có thể hủy yêu cầu nạp vào ví.",
                "already_paid": "Yêu cầu này đã được thanh toán nên không thể hủy.",
                "already_credited": "Tiền của yêu cầu này đã được cộng nên không thể hủy.",
                "already_cancelled": "Yêu cầu nạp này đã được hủy trước đó.",
                "invalid_status": "Trạng thái yêu cầu không thể hủy.",
                "user_not_found": "Không tìm thấy khách hàng của yêu cầu nạp.",
            }
            message = messages.get(result.status, "Không thể hủy yêu cầu nạp.")
            if wants_json:
                return JSONResponse(
                    {"ok": False, "status": result.status, "message": message},
                    status_code=409,
                )
            flash(request, message, "error")
            return RedirectResponse("/admin/payments", status_code=303)

        message = f"Đã hủy yêu cầu nạp {format_vnd(result.amount)} mã {result.deposit_code}."
        if not wants_json:
            flash(request, message)
        if wants_json:
            return JSONResponse(
                {
                    "ok": True,
                    "status": "cancelled",
                    "deposit_id": deposit_id,
                    "message": message,
                }
            )
        return RedirectResponse("/admin/payments", status_code=303)

    @router.get("/admin/sms-rentals", response_class=HTMLResponse)
    async def sms_rentals_page(
        request: Request,
        status: str = "all",
        q: str = "",
        page: int = 1,
    ) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        selected_status = (
            status
            if status in {"all", "pending", "unknown", "success", "refunded"}
            else "all"
        )
        search = q.strip()[:100]
        rental_conditions = []
        if selected_status == "pending":
            rental_conditions.append(
                SmsRental.status.in_(("requesting", "pending"))
            )
        elif selected_status != "all":
            rental_conditions.append(SmsRental.status == selected_status)
        if search:
            pattern = f"%{search}%"
            rental_conditions.append(
                or_(
                    cast(SmsRental.id, String).ilike(pattern),
                    cast(SmsRental.user_id, String).ilike(pattern),
                    SmsRental.shop_order_code.ilike(pattern),
                    SmsRental.provider_order_id.ilike(pattern),
                    SmsRental.phone_number.ilike(pattern),
                    SmsRental.otp_code.ilike(pattern),
                    User.full_name.ilike(pattern),
                    User.username.ilike(pattern),
                )
            )
        async with session_factory() as session:
            rental_count_statement = (
                select(func.count(SmsRental.id))
                .join(User, User.telegram_id == SmsRental.user_id)
            )
            if rental_conditions:
                rental_count_statement = rental_count_statement.where(
                    *rental_conditions
                )
            rental_count = int(
                await session.scalar(rental_count_statement) or 0
            )
            pager = admin_pager(request, rental_count, page)
            statement = (
                select(SmsRental, User)
                .join(User, User.telegram_id == SmsRental.user_id)
                .order_by(SmsRental.id.desc())
                .offset(pager.offset)
                .limit(ADMIN_PAGE_SIZE)
            )
            if rental_conditions:
                statement = statement.where(*rental_conditions)
            rentals = [
                {"rental": rental, "user": user}
                for rental, user in await session.execute(statement)
            ]
            metrics = (
                await session.execute(
                    select(
                        func.count(SmsRental.id),
                        func.count(SmsRental.id).filter(
                            SmsRental.status.in_(("requesting", "pending"))
                        ),
                        func.count(SmsRental.id).filter(SmsRental.status == "unknown"),
                        func.count(SmsRental.id).filter(SmsRental.status == "success"),
                        func.count(SmsRental.id).filter(SmsRental.status == "refunded"),
                        func.count(func.distinct(SmsRental.user_id)),
                        func.coalesce(
                            func.sum(SmsRental.sale_amount).filter(
                                SmsRental.status == "success"
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(SmsRental.cost_amount).filter(
                                SmsRental.status == "success"
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(SmsRental.sale_amount).filter(
                                SmsRental.status == "refunded"
                            ),
                            0,
                        ),
                    )
                )
            ).one()
            referral = int(
                await session.scalar(
                    select(
                        func.coalesce(func.sum(ReferralReward.commission_amount), 0)
                    ).where(
                        ReferralReward.shop_order_code.in_(
                            select(SmsRental.shop_order_code).where(
                                SmsRental.status == "success",
                                SmsRental.shop_order_code.is_not(None),
                            )
                        )
                    )
                )
                or 0
            )
        availability = await sms_availability(
            rentsim_client,
            settings.rentsim_markup,
            fallback_unit_cost=settings.rentsim_fallback_price,
        )
        total, pending, unknown, success, refunded, users, revenue, cost, refund_total = (
            int(value) for value in metrics
        )
        return templates.TemplateResponse(
            request,
            "sms_rentals.html",
            page_context(
                request,
                "Thuê số SMS",
                "sms-rentals",
                rentals=rentals,
                selected_status=selected_status,
                search=search,
                pager=pager,
                availability=availability,
                stats={
                    "total": total,
                    "pending": pending,
                    "unknown": unknown,
                    "success": success,
                    "refunded": refunded,
                    "users": users,
                    "revenue": revenue,
                    "cost": cost,
                    "referral": referral,
                    "profit": revenue - cost - referral,
                    "refund_total": refund_total,
                },
            ),
        )

    @router.get("/admin/supplier-audit", response_class=HTMLResponse)
    async def supplier_audit_page(
        request: Request,
        provider: str = PROVIDER,
        kind: str = "all",
        transaction_page: int = 1,
        attempt_page: int = 1,
    ) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        provider_clients = {
            PROVIDER: supplier_client,
            "lehai": lehai_client,
            "canboso": canboso_client,
            "nce": nce_client,
            "haji": haji_client,
        }
        provider_labels = {
            PROVIDER: "Sumi",
            "lehai": "Lê Hải Premium",
            "canboso": "Canboso",
            "nce": "API Codex & Claude",
            "haji": "Haji",
        }
        selected_provider = provider if provider in provider_clients else PROVIDER
        provider_label = provider_labels[selected_provider]
        selected_client = provider_clients[selected_provider]
        selected_kind = (
            kind
            if kind
            in {"all", "suspicious", "recovered", "refunded", "purchase", "credit"}
            else "all"
        )
        async with session_factory() as session:
            state = await session.get(SupplierBalanceState, selected_provider)
            transaction_conditions = [
                SupplierBalanceTransaction.provider == selected_provider
            ]
            if selected_kind != "all":
                transaction_conditions.append(
                    SupplierBalanceTransaction.kind == selected_kind
                )
            transaction_count = int(
                await session.scalar(
                    select(func.count(SupplierBalanceTransaction.id)).where(
                        *transaction_conditions
                    )
                )
                or 0
            )
            attempt_count = int(
                await session.scalar(
                    select(func.count(SupplierPurchaseAttempt.id)).where(
                        SupplierPurchaseAttempt.provider == selected_provider
                    )
                )
                or 0
            )
            transaction_pager = admin_pager(
                request,
                transaction_count,
                transaction_page,
                page_parameter="transaction_page",
            )
            attempt_pager = admin_pager(
                request,
                attempt_count,
                attempt_page,
                page_parameter="attempt_page",
            )
            statement = (
                select(SupplierBalanceTransaction)
                .where(*transaction_conditions)
                .order_by(SupplierBalanceTransaction.id.desc())
                .offset(transaction_pager.offset)
                .limit(ADMIN_PAGE_SIZE)
            )
            transactions = list(await session.scalars(statement))
            purchase_attempts = (
                await session.execute(
                    select(SupplierPurchaseAttempt, Product)
                    .outerjoin(Product, Product.id == SupplierPurchaseAttempt.product_id)
                    .where(SupplierPurchaseAttempt.provider == selected_provider)
                    .order_by(SupplierPurchaseAttempt.id.desc())
                    .offset(attempt_pager.offset)
                    .limit(ADMIN_PAGE_SIZE)
                )
            ).all()
            suspicious_count, suspicious_sum = (
                await session.execute(
                    select(
                        func.count(SupplierBalanceTransaction.id),
                        func.coalesce(func.sum(SupplierBalanceTransaction.amount), 0),
                    ).where(
                        SupplierBalanceTransaction.provider == selected_provider,
                        SupplierBalanceTransaction.kind == "suspicious",
                    )
                )
            ).one()
            purchase_count, purchase_sum = (
                await session.execute(
                    select(
                        func.count(SupplierBalanceTransaction.id),
                        func.coalesce(func.sum(SupplierBalanceTransaction.amount), 0),
                    ).where(
                        SupplierBalanceTransaction.provider == selected_provider,
                        SupplierBalanceTransaction.kind == "purchase",
                    )
                )
            ).one()
            credit_sum = int(
                await session.scalar(
                    select(func.coalesce(func.sum(SupplierBalanceTransaction.amount), 0)).where(
                        SupplierBalanceTransaction.provider == selected_provider,
                        SupplierBalanceTransaction.kind == "credit",
                    )
                )
                or 0
            )
        return templates.TemplateResponse(
            request,
            "supplier_audit.html",
            page_context(
                request,
                "Giao dịch đáng ngờ",
                "supplier-audit",
                transactions=transactions,
                selected_kind=selected_kind,
                selected_provider=selected_provider,
                provider_label=provider_label,
                source_usd_rate=settings.canboso_usd_to_vnd,
                supplier_connected=selected_client is not None,
                purchase_attempts=purchase_attempts,
                transaction_pager=transaction_pager,
                attempt_pager=attempt_pager,
                stats={
                    "current_balance": state.last_balance if state else None,
                    "last_checked": state.checked_at if state else None,
                    "suspicious_count": int(suspicious_count),
                    "suspicious_total": abs(int(suspicious_sum)),
                    "purchase_count": int(purchase_count),
                    "purchase_total": abs(int(purchase_sum)),
                    "credit_total": credit_sum,
                },
            ),
        )

    @router.post("/admin/supplier-audit/reconcile")
    async def reconcile_supplier_audit(
        request: Request,
        csrf: str = Form(...),
        provider: str = Form(PROVIDER),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/supplier-audit", status_code=303)
        provider_clients = {
            PROVIDER: supplier_client,
            "lehai": lehai_client,
            "canboso": canboso_client,
            "nce": nce_client,
            "haji": haji_client,
        }
        provider_labels = {
            PROVIDER: "Sumi",
            "lehai": "Lê Hải Premium",
            "canboso": "Canboso",
            "nce": "API Codex & Claude",
            "haji": "Haji",
        }
        selected_provider = provider if provider in provider_clients else PROVIDER
        provider_label = provider_labels[selected_provider]
        selected_client = provider_clients[selected_provider]
        redirect_url = f"/admin/supplier-audit?provider={selected_provider}"
        if selected_client is None:
            flash(
                request,
                f"{provider_label} chưa được kết nối nên không thể đối soát.",
                "error",
            )
            return RedirectResponse(redirect_url, status_code=303)
        try:
            result = await reconcile_supplier_balance(
                session_factory,
                selected_client,
                provider=selected_provider,
                provider_label=provider_label,
            )
        except SupplierError:
            flash(
                request,
                f"Không lấy được số dư {provider_label}. Hãy thử lại sau.",
                "error",
            )
        else:
            if result.initialized:
                flash(
                    request,
                    f"Đã lưu số dư {provider_label} làm mốc đối soát ban đầu.",
                )
            elif result.refunded_amount > 0:
                flash(
                    request,
                    f"Đã tự động đối chiếu khoản hoàn {format_vnd(result.refunded_amount)} "
                    f"với {len(result.refunded_audit_ids)} giao dịch lỗi.",
                )
            elif result.suspicious_amount < 0:
                flash(
                    request,
                    f"Phát hiện giao dịch đáng ngờ -{format_vnd(abs(result.suspicious_amount))}.",
                    "error",
                )
            else:
                flash(request, "Đối soát hoàn tất, không có khoản giảm bất thường.")
        return RedirectResponse(redirect_url, status_code=303)

    @router.get("/admin/system", response_class=HTMLResponse)
    async def system_page(request: Request) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        webhook_url = (
            settings.public_base_url.rstrip("/") + "/webhooks/sepay"
            if settings.public_base_url
            else "/webhooks/sepay"
        )
        return templates.TemplateResponse(
            request,
            "system.html",
            page_context(
                request,
                "Cấu hình hệ thống",
                "system",
                settings=settings,
                webhook_url=webhook_url,
            ),
        )

    return router
