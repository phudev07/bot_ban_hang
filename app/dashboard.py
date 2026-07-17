import hmac
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, cast, delete, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.config import Settings
from app.models import (
    BalanceAdjustment,
    BroadcastLog,
    ApiClient,
    ApiRequestAudit,
    Category,
    Deposit,
    DiscountCode,
    InventoryItem,
    Order,
    PaymentTransaction,
    Product,
    ReferralReward,
    SupplierBalanceState,
    SupplierBalanceTransaction,
    User,
)
from app.supplier_audit import PROVIDER, reconcile_supplier_balance
from app.suppliers import SumistoreClient, SupplierError
from app.utils import SecretCipher, format_vnd, parse_vnd
from app.dashboard_security import new_csrf_token, verify_dashboard_password


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["vnd"] = format_vnd


LOCAL_TIMEZONE = ZoneInfo("Asia/Bangkok")


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
        func.coalesce(func.sum(Order.amount), 0),
        func.coalesce(func.sum(Order.cost_amount), 0),
        func.coalesce(func.sum(Order.discount_amount), 0),
    ).join(Product, Product.id == Order.product_id).where(
        Order.status == "completed",
        Product.fulfillment_source.in_(("local", "sumistore")),
        Product.product_type == "account",
    )
    if start_at is not None:
        statement = statement.where(Order.created_at >= start_at)
    order_count, revenue, cost, discount = (await session.execute(statement)).one()
    revenue = int(revenue)
    cost = int(cost)
    reward_statement = select(
        func.coalesce(func.sum(ReferralReward.commission_amount), 0)
    )
    if start_at is not None:
        reward_statement = reward_statement.where(ReferralReward.created_at >= start_at)
    referral = int(await session.scalar(reward_statement) or 0)
    gross_profit = revenue - cost
    profit = gross_profit - referral
    return {
        "orders": int(order_count),
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

    def sum_since(column, start_at: datetime):
        return func.coalesce(func.sum(column).filter(Order.created_at >= start_at), 0)

    statement = select(
        count_since(periods["today"]).label("today_orders"),
        sum_since(Order.amount, periods["today"]).label("today_revenue"),
        sum_since(Order.cost_amount, periods["today"]).label("today_cost"),
        sum_since(Order.discount_amount, periods["today"]).label("today_discount"),
        count_since(periods["month"]).label("month_orders"),
        sum_since(Order.amount, periods["month"]).label("month_revenue"),
        sum_since(Order.cost_amount, periods["month"]).label("month_cost"),
        sum_since(Order.discount_amount, periods["month"]).label("month_discount"),
        count_since(periods["year"]).label("year_orders"),
        sum_since(Order.amount, periods["year"]).label("year_revenue"),
        sum_since(Order.cost_amount, periods["year"]).label("year_cost"),
        sum_since(Order.discount_amount, periods["year"]).label("year_discount"),
        purchase_order_count().label("all_orders"),
        func.coalesce(func.sum(Order.amount), 0).label("all_revenue"),
        func.coalesce(func.sum(Order.cost_amount), 0).label("all_cost"),
        func.coalesce(func.sum(Order.discount_amount), 0).label("all_discount"),
    ).join(Product, Product.id == Order.product_id).where(
        Order.status == "completed",
        Product.fulfillment_source.in_(("local", "sumistore")),
        Product.product_type == "account",
    )
    values = (await session.execute(statement)).one()
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
    reward_fields = reward_values._mapping
    result: dict[str, dict[str, int | float]] = {}
    for key in ("today", "month", "year", "all"):
        revenue = int(fields[f"{key}_revenue"])
        cost = int(fields[f"{key}_cost"])
        referral = int(reward_fields[f"{key}_referral"])
        gross_profit = revenue - cost
        profit = gross_profit - referral
        result[key] = {
            "orders": int(fields[f"{key}_orders"]),
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
    return normalized if normalized in {"local", "sumistore"} else None


def create_dashboard_router(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    cipher: SecretCipher,
    supplier_client: SumistoreClient | None = None,
) -> APIRouter:
    router = APIRouter()

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
                        Product.product_type == "account",
                    )
                )
                or 0
            )
            stock += int(
                await session.scalar(
                    select(func.coalesce(func.sum(Product.external_stock), 0)).where(
                        Product.fulfillment_source == "sumistore"
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
            buying_users = int(
                await session.scalar(
                    select(func.count(func.distinct(Order.user_id)))
                    .join(Product, Product.id == Order.product_id)
                    .where(
                        Product.fulfillment_source.in_(("local", "sumistore")),
                        Product.product_type == "account",
                    )
                )
                or 0
            )
            rows = await session.execute(
                select(Order, Product, User)
                .join(Product, Product.id == Order.product_id)
                .join(User, User.telegram_id == Order.user_id)
                .where(
                    Product.fulfillment_source.in_(("local", "sumistore")),
                    Product.product_type == "account",
                )
                .order_by(Order.id.desc())
                .limit(800)
            )
            recent_orders = group_order_rows(rows, limit=8)
            recent_users = list(
                await session.scalars(select(User).order_by(User.created_at.desc()).limit(6))
            )
            top_product_rows = await session.execute(
                select(
                    Product,
                    purchase_order_count(),
                    func.coalesce(func.sum(Order.amount), 0),
                    func.coalesce(func.sum(Order.cost_amount), 0),
                    func.coalesce(func.sum(Order.discount_amount), 0),
                )
                .join(Order, Order.product_id == Product.id)
                .where(
                    Order.status == "completed",
                    Product.fulfillment_source.in_(("local", "sumistore")),
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
                    "revenue": int(total),
                    "cost": int(cost),
                    "profit": int(total) - int(cost),
                    "discount": int(discount),
                }
                for product, count, total, cost, discount in top_product_rows
            ]
            sales_rows = await session.execute(
                select(Order.created_at, Order.amount, Order.cost_amount)
                .join(Product, Product.id == Order.product_id)
                .where(
                    Order.created_at >= periods["fourteen_days"],
                    Order.status == "completed",
                    Product.fulfillment_source.in_(("local", "sumistore")),
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
                    Product.fulfillment_source.in_(("local", "sumistore")),
                    Product.product_type == "account",
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
                await session.scalars(select(Category).order_by(Category.position, Category.id))
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
                    select(func.count(Product.id)).where(Product.category_id == category_id)
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
            await session.delete(category)
            await session.commit()
        flash(request, "Đã xóa gian hàng trống.")
        return RedirectResponse("/admin/categories", status_code=303)

    async def product_rows(session: AsyncSession) -> list[dict[str, object]]:
        stock_query = (
            select(
                InventoryItem.product_id,
                func.count(InventoryItem.id).label("stock"),
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
                func.coalesce(coupon_query.c.coupon_count, 0),
            )
            .join(Category, Category.id == Product.category_id)
            .outerjoin(stock_query, stock_query.c.product_id == Product.id)
            .outerjoin(coupon_query, coupon_query.c.product_id == Product.id)
            .where(
                Product.fulfillment_source.in_(("local", "sumistore")),
                Product.product_type == "account",
            )
            .order_by(Product.id.desc())
        )
        return [
            {
                "product": product,
                "category": category,
                "stock": (
                    max(0, product.external_stock)
                    if product.fulfillment_source == "sumistore"
                    else int(stock)
                ),
                "coupon_count": int(coupon_count),
                "unit_cost": (
                    int(product.supplier_price or 0)
                    if product.fulfillment_source == "sumistore"
                    else 0
                ),
                "unit_profit": (
                    product.price - int(product.supplier_price or 0)
                    if product.fulfillment_source == "sumistore"
                    else product.price
                ),
            }
            for product, category, stock, coupon_count in rows
        ]

    @router.get("/admin/products", response_class=HTMLResponse)
    async def products_page(request: Request) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        async with session_factory() as session:
            products = await product_rows(session)
            categories = list(
                await session.scalars(select(Category).order_by(Category.position, Category.id))
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
            or (normalized_source == "sumistore" and not normalized_supplier_id)
        ):
            flash(request, "Thông tin sản phẩm không hợp lệ.", "error")
            return RedirectResponse("/admin/products", status_code=303)
        async with session_factory() as session:
            if await session.get(Category, category_id) is None:
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
                        normalized_supplier_id if normalized_source == "sumistore" else None
                    ),
                    supplier_markup=(parsed_markup if normalized_source == "sumistore" else 0),
                    supplier_price=None,
                    external_stock=0,
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
                await session.scalars(select(Category).order_by(Category.position, Category.id))
            )
        if (
            product is None
            or product.fulfillment_source not in {"local", "sumistore"}
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
        supplier_markup: str = Form("0"),
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
        parsed_markup = parse_vnd(supplier_markup) or 0
        normalized_supplier_id = supplier_product_id.strip() or None
        async with session_factory() as session:
            product = await session.get(Product, product_id)
            category = await session.get(Category, category_id)
            if (
                product is None
                or category is None
                or not normalized_name
                or not parsed_price
                or normalized_type is None
                or normalized_source is None
                or (normalized_source == "sumistore" and not normalized_supplier_id)
            ):
                flash(request, "Không thể cập nhật sản phẩm.", "error")
                return RedirectResponse("/admin/products", status_code=303)
            product.category_id = category_id
            product.name_vi = normalized_name
            product.name_en = name_en.strip() or normalized_name
            product.price = parsed_price
            product.description_vi = description_vi.strip()
            product.description_en = description_en.strip() or description_vi.strip()
            product.product_type = normalized_type
            product.fulfillment_source = normalized_source
            product.supplier_product_id = (
                normalized_supplier_id if normalized_source == "sumistore" else None
            )
            product.supplier_markup = parsed_markup if normalized_source == "sumistore" else 0
            if normalized_source == "local":
                product.supplier_price = None
                product.external_stock = 0
            product.allow_quantity = allow_quantity is not None
            product.max_quantity = max(1, min(max_quantity, 100))
            product.active = active is not None
            await session.commit()
        flash(request, "Đã lưu thông tin sản phẩm.")
        return RedirectResponse(f"/admin/products/{product_id}", status_code=303)

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
                flash(
                    request,
                    "Sản phẩm đã có đơn hoặc thanh toán nên không thể xóa. "
                    "Hãy tắt trạng thái hiển thị để giữ lịch sử.",
                    "error",
                )
                return RedirectResponse(f"/admin/products/{product_id}", status_code=303)
            await session.execute(
                delete(InventoryItem).where(InventoryItem.product_id == product_id)
            )
            await session.execute(
                delete(DiscountCode).where(DiscountCode.product_id == product_id)
            )
            await session.delete(product)
            await session.commit()
        flash(request, "Đã xóa sản phẩm và toàn bộ kho chưa bán của sản phẩm đó.")
        return RedirectResponse("/admin/products", status_code=303)

    @router.get("/admin/discounts", response_class=HTMLResponse)
    async def discounts_page(request: Request, product_id: int | None = None) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        async with session_factory() as session:
            products = list(
                await session.scalars(
                    select(Product)
                    .where(
                        Product.fulfillment_source.in_(("local", "sumistore")),
                        Product.product_type == "account",
                    )
                    .order_by(Product.name_vi, Product.id)
                )
            )
            statement = (
                select(DiscountCode, Product)
                .join(Product, Product.id == DiscountCode.product_id)
                .where(
                    Product.fulfillment_source.in_(("local", "sumistore")),
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
            active_count = int(
                await session.scalar(
                    select(func.count(DiscountCode.id))
                    .join(Product, Product.id == DiscountCode.product_id)
                    .where(
                        DiscountCode.active.is_(True),
                        Product.fulfillment_source.in_(("local", "sumistore")),
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
                        Product.fulfillment_source.in_(("local", "sumistore")),
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
        return templates.TemplateResponse(
            request,
            "discounts.html",
            page_context(
                request,
                "Mã giảm giá",
                "discounts",
                products=products,
                codes=codes,
                selected_product_id=product_id,
                stats={
                    "active": active_count,
                    "uses": total_uses,
                    "discount": total_discount,
                },
            ),
        )

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
                or product.fulfillment_source not in {"local", "sumistore"}
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
    async def inventory_page(request: Request) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        async with session_factory() as session:
            products = await product_rows(session)
            inventory_rows = await session.execute(
                select(InventoryItem, Product)
                .join(Product, Product.id == InventoryItem.product_id)
                .where(
                    Product.fulfillment_source.in_(("local", "sumistore")),
                    Product.product_type == "account",
                )
                .order_by(InventoryItem.id.desc())
                .limit(100)
            )
            recent_items = [{"item": item, "product": product} for item, product in inventory_rows]
        return templates.TemplateResponse(
            request,
            "inventory.html",
            page_context(
                request,
                "Nhập kho",
                "inventory",
                products=products,
                import_products=[
                    row for row in products if row["product"].fulfillment_source == "local"
                ],
                recent_items=recent_items,
            ),
        )

    @router.post("/admin/inventory")
    async def add_inventory(
        request: Request,
        csrf: str = Form(...),
        product_id: int = Form(...),
        items: str = Form(...),
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/inventory", status_code=303)
        parsed_items = split_inventory_items(items)
        async with session_factory() as session:
            product = await session.get(Product, product_id)
            if (
                product is None
                or product.fulfillment_source != "local"
                or product.product_type != "account"
                or not parsed_items
            ):
                flash(request, "Sản phẩm hoặc dữ liệu kho không hợp lệ.", "error")
                return RedirectResponse("/admin/inventory", status_code=303)
            session.add_all(
                [
                    InventoryItem(
                        product_id=product.id,
                        encrypted_secret=cipher.encrypt(item),
                    )
                    for item in parsed_items
                ]
            )
            await session.commit()
        flash(request, f"Đã thêm {len(parsed_items)} sản phẩm vào kho.")
        return RedirectResponse("/admin/inventory", status_code=303)

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
            item = await session.get(InventoryItem, item_id)
            if item is None or item.status != "available":
                flash(request, "Chỉ có thể xóa mục kho chưa bán.", "error")
                return RedirectResponse("/admin/inventory", status_code=303)
            await session.delete(item)
            await session.commit()
        flash(request, f"Đã xóa mục kho #{item_id}.")
        return RedirectResponse("/admin/inventory", status_code=303)

    @router.get("/admin/users", response_class=HTMLResponse)
    async def users_page(request: Request, q: str = "", status: str = "all") -> Response:
        if not is_admin(request):
            return redirect_to_login()
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
            deposit_stats = (
                select(
                    PaymentTransaction.user_id.label("user_id"),
                    func.coalesce(func.sum(PaymentTransaction.amount), 0).label("deposited"),
                    func.max(PaymentTransaction.created_at).label("last_deposit_at"),
                )
                .where(PaymentTransaction.credit_status == "credited")
                .group_by(PaymentTransaction.user_id)
                .subquery()
            )
            statement = (
                select(
                    User,
                    func.coalesce(order_stats.c.order_count, 0),
                    func.coalesce(order_stats.c.spent, 0),
                    order_stats.c.last_order_at,
                    func.coalesce(deposit_stats.c.deposited, 0),
                    deposit_stats.c.last_deposit_at,
                )
                .outerjoin(order_stats, order_stats.c.user_id == User.telegram_id)
                .outerjoin(deposit_stats, deposit_stats.c.user_id == User.telegram_id)
                .order_by(User.created_at.desc())
                .limit(200)
            )
            if q.strip():
                needle = f"%{q.strip()}%"
                statement = statement.where(
                    or_(
                        User.username.ilike(needle),
                        User.full_name.ilike(needle),
                        cast(User.telegram_id, String).ilike(needle),
                    )
                )
            if status == "blocked":
                statement = statement.where(User.is_blocked.is_(True))
            elif status == "started":
                statement = statement.where(User.has_started.is_(True))
            elif status == "inactive":
                statement = statement.where(User.has_started.is_(False))
            user_rows = [
                {
                    "user": user,
                    "order_count": int(order_count),
                    "spent": int(spent),
                    "last_order_at": last_order_at,
                    "deposited": int(deposited),
                    "last_deposit_at": last_deposit_at,
                }
                for user, order_count, spent, last_order_at, deposited, last_deposit_at
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
            ),
        )

    @router.get("/admin/broadcasts", response_class=HTMLResponse)
    async def broadcasts_page(request: Request) -> Response:
        if not is_admin(request):
            return redirect_to_login()
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
            broadcasts = list(
                await session.scalars(
                    select(BroadcastLog).order_by(BroadcastLog.id.desc()).limit(100)
                )
            )
        return templates.TemplateResponse(
            request,
            "broadcasts.html",
            page_context(
                request,
                "Thông báo",
                "broadcasts",
                active_recipients=active_recipients,
                broadcast_count=broadcast_count,
                delivered_count=delivered_count,
                failed_count=failed_count,
                broadcasts=broadcasts,
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
        async with session_factory() as session:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.telegram_id == user_id).with_for_update()
                )
                if user is None or adjustment == 0 or user.balance + adjustment < 0:
                    flash(request, "Không thể điều chỉnh số dư.", "error")
                    return RedirectResponse("/admin/users", status_code=303)
                user.balance += adjustment
                session.add(
                    BalanceAdjustment(
                        user_id=user.telegram_id,
                        admin_username=str(request.session["dashboard_admin"]),
                        amount=adjustment,
                        reason=reason.strip(),
                    )
                )
        flash(request, "Đã cập nhật số dư và ghi lịch sử audit.")
        return RedirectResponse("/admin/users", status_code=303)

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
    ) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        conditions = [
            Product.fulfillment_source.in_(("local", "sumistore")),
            Product.product_type == "account",
        ]
        search_condition = None
        periods = dashboard_periods()
        if q.strip():
            needle = f"%{q.strip()}%"
            search_condition = or_(
                cast(Order.id, String).ilike(needle),
                Order.batch_code.ilike(needle),
                Order.supplier_order_code.ilike(needle),
                cast(User.telegram_id, String).ilike(needle),
                User.full_name.ilike(needle),
                User.username.ilike(needle),
                Product.name_vi.ilike(needle),
            )
        if status in {"completed", "pending", "failed"}:
            conditions.append(Order.status == status)
        if source in {"local", "sumistore"}:
            conditions.append(Product.fulfillment_source == source)
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
            statement = (
                select(Order, Product, User)
                .join(Product, Product.id == Order.product_id)
                .join(User, User.telegram_id == Order.user_id)
                .order_by(Order.id.desc())
                .limit(30_000)
            )
            if conditions:
                statement = statement.where(*conditions)
            if matching_keys is not None:
                statement = statement.where(
                    order_group_key().in_(select(matching_keys.c.group_key))
                )
            rows = await session.execute(statement)
            orders = group_order_rows(rows, limit=300)
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
                        Product.fulfillment_source.in_(("local", "sumistore")),
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
                        Product.fulfillment_source.in_(("local", "sumistore")),
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
                        Product.fulfillment_source.in_(("local", "sumistore")),
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
                .order_by(request_stats.c.last_request_at.desc(), ApiClient.id.desc())
                .limit(300)
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
                query=q,
                status=selected_status,
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
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/api-clients", status_code=303)
        async with session_factory() as session:
            client = await session.get(ApiClient, client_id)
            if client is not None:
                client.rate_limit_per_minute = max(1, min(rate_limit_per_minute, 10_000))
                client.allowed_ips = allowed_ips.strip()[:2000]
                client.admin_blocked = admin_blocked is not None
                await session.commit()
                flash(request, "Đã cập nhật API client.")
        selected_status = (
            return_status
            if return_status in {"all", "active", "paused", "blocked", "attention"}
            else "all"
        )
        query_string = urlencode({"q": return_q.strip(), "status": selected_status})
        return RedirectResponse(f"/admin/api-clients?{query_string}", status_code=303)

    @router.get("/admin/api-orders")
    async def api_orders_redirect(request: Request) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        return RedirectResponse("/admin/orders?channel=api", status_code=303)

    @router.get("/admin/referrals", response_class=HTMLResponse)
    async def referrals_page(request: Request) -> Response:
        if not is_admin(request):
            return redirect_to_login()
        referrer = aliased(User)
        referred = aliased(User)
        async with session_factory() as session:
            rewards = [
                {"reward": reward, "referrer": source, "referred": target}
                for reward, source, target in await session.execute(
                    select(ReferralReward, referrer, referred)
                    .join(referrer, referrer.telegram_id == ReferralReward.referrer_user_id)
                    .join(referred, referred.telegram_id == ReferralReward.referred_user_id)
                    .order_by(ReferralReward.id.desc())
                    .limit(300)
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
            ),
        )

    @router.get("/admin/payments", response_class=HTMLResponse)
    async def payments_page(
        request: Request,
        q: str = "",
        status: str = "all",
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
        if status in {"pending", "paid", "failed"}:
            deposit_conditions.append(Deposit.status == status)
        periods = dashboard_periods()
        async with session_factory() as session:
            deposit_statement = (
                select(Deposit, User)
                .join(User, User.telegram_id == Deposit.user_id)
                .order_by(Deposit.id.desc())
                .limit(200)
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
                    .limit(200)
                )
            ]
            adjustments = [
                {"adjustment": adjustment, "user": user}
                for adjustment, user in await session.execute(
                    select(BalanceAdjustment, User)
                    .join(User, User.telegram_id == BalanceAdjustment.user_id)
                    .order_by(BalanceAdjustment.id.desc())
                    .limit(150)
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
                        PaymentTransaction.credit_status != "credited"
                    )
                )
                or 0
            )
            review_count = int(
                await session.scalar(
                    select(func.count(PaymentTransaction.id)).where(
                        PaymentTransaction.credit_status != "credited"
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

    @router.get("/admin/supplier-audit", response_class=HTMLResponse)
    async def supplier_audit_page(request: Request, kind: str = "all") -> Response:
        if not is_admin(request):
            return redirect_to_login()
        selected_kind = kind if kind in {"all", "suspicious", "purchase", "credit"} else "all"
        async with session_factory() as session:
            state = await session.get(SupplierBalanceState, PROVIDER)
            statement = (
                select(SupplierBalanceTransaction)
                .where(SupplierBalanceTransaction.provider == PROVIDER)
                .order_by(SupplierBalanceTransaction.id.desc())
                .limit(300)
            )
            if selected_kind != "all":
                statement = statement.where(SupplierBalanceTransaction.kind == selected_kind)
            transactions = list(await session.scalars(statement))
            suspicious_count, suspicious_sum = (
                await session.execute(
                    select(
                        func.count(SupplierBalanceTransaction.id),
                        func.coalesce(func.sum(SupplierBalanceTransaction.amount), 0),
                    ).where(
                        SupplierBalanceTransaction.provider == PROVIDER,
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
                        SupplierBalanceTransaction.provider == PROVIDER,
                        SupplierBalanceTransaction.kind == "purchase",
                    )
                )
            ).one()
            credit_sum = int(
                await session.scalar(
                    select(func.coalesce(func.sum(SupplierBalanceTransaction.amount), 0)).where(
                        SupplierBalanceTransaction.provider == PROVIDER,
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
                supplier_connected=supplier_client is not None,
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
    ) -> RedirectResponse:
        if not is_admin(request):
            return redirect_to_login()
        if not valid_csrf(request, csrf):
            return RedirectResponse("/admin/supplier-audit", status_code=303)
        if supplier_client is None:
            flash(request, "Sumi chưa được kết nối nên không thể đối soát.", "error")
            return RedirectResponse("/admin/supplier-audit", status_code=303)
        try:
            result = await reconcile_supplier_balance(session_factory, supplier_client)
        except SupplierError:
            flash(request, "Không lấy được số dư Sumi. Hãy thử lại sau.", "error")
        else:
            if result.initialized:
                flash(request, "Đã lưu số dư Sumi làm mốc đối soát ban đầu.")
            elif result.suspicious_amount < 0:
                flash(
                    request,
                    f"Phát hiện giao dịch đáng ngờ -{format_vnd(abs(result.suspicious_amount))}.",
                    "error",
                )
            else:
                flash(request, "Đối soát hoàn tất, không có khoản giảm bất thường.")
        return RedirectResponse("/admin/supplier-audit", status_code=303)

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
