from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import BalanceAdjustment, SmsRental, User
from app.partner_services import award_referral_commission
from app.rentsim import RentSimClient, RentSimError, RentSimSnapshot
from app.wallet_ledger import apply_wallet_change


ACTIVE_SMS_STATUSES = ("requesting", "pending", "unknown")
AMBIGUOUS_RENT_ERRORS = {"PROVIDER_UNAVAILABLE", "INVALID_RESPONSE"}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(frozen=True)
class SmsAvailability:
    connected: bool
    service_name: str = "ChatGPT"
    server_id: str = "kh2"
    unit_cost: int = 0
    sale_price: int = 0
    source_stock: int = 0
    effective_stock: int = 0
    provider_balance: int = 0
    error_code: str | None = None


@dataclass(frozen=True)
class SmsRentResult:
    ok: bool
    message: str
    rental_id: int | None = None
    shop_order_code: str | None = None
    phone_number: str = ""
    phone_number_display: str = ""
    sale_amount: int = 0
    cost_amount: int = 0
    balance: int = 0
    status: str = ""
    otp_code: str = ""
    otp_content: str = ""
    retry_after: int = 0
    provider_balance_before: int | None = None
    provider_balance_after: int | None = None


@dataclass(frozen=True)
class SmsPollNotification:
    rental_id: int
    user_id: int
    rental_message_id: int | None
    waiting_message_id: int | None
    status: str
    shop_order_code: str
    phone_number: str
    sale_amount: int
    balance: int
    language: str = "vi"
    otp_code: str = ""
    otp_content: str = ""
    failure_reason: str = ""


@dataclass(frozen=True)
class SmsReviewAlert:
    rental_id: int
    user_id: int
    status: str
    shop_order_code: str
    phone_number: str
    requested_at: datetime
    provider_balance_before: int | None
    provider_balance_after: int | None
    last_error: str
    poll_attempts: int


def _is_ambiguous_rent_error(code: str) -> bool:
    if code in AMBIGUOUS_RENT_ERRORS:
        return True
    # RentSim uses HTTP 500 when the rent request itself fails before an
    # order/phone number is created. This is refundable, not ambiguous.
    if code == "PROVIDER_HTTP_500":
        return False
    if not code.startswith("PROVIDER_HTTP_"):
        return False
    try:
        return int(code.rsplit("_", 1)[1]) >= 500
    except ValueError:
        return False


async def sms_availability(
    client: RentSimClient | None,
    markup: int,
    *,
    fallback_unit_cost: int = 1_000,
    force: bool = False,
) -> SmsAvailability:
    fallback_sale_price = max(0, fallback_unit_cost) + max(0, markup)
    if client is None:
        return SmsAvailability(
            False,
            unit_cost=max(0, fallback_unit_cost),
            sale_price=fallback_sale_price,
            error_code="DISABLED",
        )
    try:
        snapshot = await client.fetch_snapshot(force=force)
    except RentSimError as exc:
        return SmsAvailability(
            False,
            unit_cost=max(0, fallback_unit_cost),
            sale_price=fallback_sale_price,
            error_code=exc.code,
        )
    return SmsAvailability(
        True,
        service_name=snapshot.service_name,
        server_id=snapshot.server_id,
        unit_cost=snapshot.unit_price,
        sale_price=snapshot.unit_price + max(0, markup),
        source_stock=snapshot.source_stock,
        effective_stock=snapshot.effective_stock,
        provider_balance=snapshot.balance,
    )


async def _refund_sms_rental(
    session: AsyncSession,
    rental: SmsRental,
    user: User,
    *,
    reason: str,
    now: datetime,
) -> None:
    if rental.refunded_at is not None or rental.status == "success":
        return
    apply_wallet_change(
        session,
        user,
        rental.sale_amount,
        kind="sms_refund",
        event_key=f"sms_refund:{rental.id}",
        reference_type="sms_rental",
        reference_id=rental.shop_order_code or f"SMS{rental.id}",
        description=(
            f"Hoàn tiền số {rental.phone_number or 'chưa được cấp'} "
            f"({rental.shop_order_code or f'SMS{rental.id}'})"
        ),
    )
    rental.status = "refunded"
    rental.failure_reason = reason[:64]
    rental.refunded_at = now
    rental.completed_at = now
    session.add(
        BalanceAdjustment(
            user_id=user.telegram_id,
            admin_username="system:rentsim",
            amount=rental.sale_amount,
            reason=(
                f"Hoàn ví thuê số SMS {rental.shop_order_code or rental.id}: {reason}"
            ),
        )
    )


def _mark_unknown_sms_rental(
    rental: SmsRental,
    *,
    reason: str,
    provider_balance_after: int | None = None,
) -> None:
    if rental.status in {"success", "refunded"}:
        return
    rental.status = "unknown"
    rental.failure_reason = reason[:64]
    rental.last_error = reason[:255]
    rental.provider_balance_after = provider_balance_after


async def _complete_sms_success(
    session: AsyncSession,
    rental: SmsRental,
    user: User,
    *,
    otp_code: str,
    otp_content: str,
    now: datetime,
    referral_commission_percent: int,
) -> None:
    if rental.status == "success":
        return
    rental.status = "success"
    rental.otp_code = otp_code[:64] or None
    rental.otp_content = otp_content or None
    rental.completed_at = now
    await award_referral_commission(
        session,
        user,
        shop_order_code=rental.shop_order_code or f"SMS{rental.id}",
        order_amount=rental.sale_amount,
        sales_channel="telegram",
        commission_percent=referral_commission_percent,
    )


async def rent_sms_number(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: int,
    client: RentSimClient | None,
    *,
    markup: int = 1_000,
    cooldown_seconds: int = 60,
    referral_commission_percent: int = 5,
    now: datetime | None = None,
) -> SmsRentResult:
    if client is None:
        return SmsRentResult(False, "disabled")
    requested_at = now or datetime.now(UTC)
    async with client.balance_lock:
        try:
            snapshot: RentSimSnapshot = await client.fetch_snapshot(force=True)
        except RentSimError as exc:
            return SmsRentResult(False, exc.code.lower())
        sale_amount = snapshot.unit_price + max(0, markup)
        if snapshot.effective_stock <= 0:
            return SmsRentResult(
                False,
                "out_of_stock",
                sale_amount=sale_amount,
                cost_amount=snapshot.unit_price,
            )

        async with session_factory() as session:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.telegram_id == user_id).with_for_update()
                )
                if user is None:
                    return SmsRentResult(False, "user_not_found")
                if user.is_blocked:
                    return SmsRentResult(False, "blocked", balance=user.balance)
                latest = await session.scalar(
                    select(SmsRental)
                    .where(SmsRental.user_id == user_id)
                    .order_by(SmsRental.requested_at.desc(), SmsRental.id.desc())
                    .limit(1)
                    .with_for_update()
                )
                latest_at = _as_utc(latest.requested_at) if latest is not None else None
                if latest is not None and latest.status == "unknown":
                    return SmsRentResult(
                        False,
                        "provider_result_unknown",
                        rental_id=latest.id,
                        shop_order_code=latest.shop_order_code,
                        balance=user.balance,
                        sale_amount=latest.sale_amount,
                        cost_amount=latest.cost_amount,
                        status=latest.status,
                        provider_balance_before=latest.provider_balance_before,
                        provider_balance_after=latest.provider_balance_after,
                    )
                if latest is not None and latest_at is not None:
                    available_at = latest_at + timedelta(seconds=cooldown_seconds)
                    if requested_at < available_at:
                        retry_after = max(
                            1,
                            int((available_at - requested_at).total_seconds()) + 1,
                        )
                        return SmsRentResult(
                            False,
                            "cooldown",
                            balance=user.balance,
                            sale_amount=sale_amount,
                            cost_amount=snapshot.unit_price,
                            retry_after=retry_after,
                        )
                    if latest.status == "requesting":
                        return SmsRentResult(
                            False,
                            "provider_result_unknown",
                            rental_id=latest.id,
                            shop_order_code=latest.shop_order_code,
                            balance=user.balance,
                            sale_amount=latest.sale_amount,
                            cost_amount=latest.cost_amount,
                            status=latest.status,
                            provider_balance_before=latest.provider_balance_before,
                            provider_balance_after=latest.provider_balance_after,
                        )
                if user.balance < sale_amount:
                    return SmsRentResult(
                        False,
                        "insufficient",
                        balance=user.balance,
                        sale_amount=sale_amount,
                        cost_amount=snapshot.unit_price,
                    )
                rental = SmsRental(
                    user_id=user.telegram_id,
                    service_id=snapshot.service_id,
                    service_name=snapshot.service_name,
                    server_id=snapshot.server_id,
                    status="requesting",
                    sale_amount=sale_amount,
                    cost_amount=snapshot.unit_price,
                    provider_balance_before=snapshot.balance,
                    source_stock=snapshot.source_stock,
                    requested_at=requested_at,
                )
                session.add(rental)
                await session.flush()
                rental.shop_order_code = f"SMS{rental.id}"
                apply_wallet_change(
                    session,
                    user,
                    -sale_amount,
                    kind="sms_rental",
                    event_key=f"sms_charge:{rental.id}",
                    reference_type="sms_rental",
                    reference_id=rental.shop_order_code,
                    description=f"Thuê số nhận OTP ChatGPT ({rental.shop_order_code})",
                )
                rental_id = rental.id
                balance_after_charge = user.balance

        try:
            provider_rental = await client.rent()
        except RentSimError as exc:
            ambiguous_result = _is_ambiguous_rent_error(exc.code)
            provider_error_refunded = exc.code == "PROVIDER_HTTP_500"
            provider_balance_after: int | None = None
            if ambiguous_result:
                try:
                    provider_balance_after = await client.fetch_balance()
                except RentSimError:
                    pass
                # A 5xx can be returned before RentSim creates an order. If the
                # provider wallet is unchanged, there is nothing to reconcile.
                if (
                    provider_balance_after is not None
                    and provider_balance_after == snapshot.balance
                ):
                    ambiguous_result = False
            async with session_factory() as session:
                async with session.begin():
                    rental = await session.scalar(
                        select(SmsRental)
                        .where(SmsRental.id == rental_id)
                        .with_for_update()
                    )
                    user = await session.scalar(
                        select(User).where(User.telegram_id == user_id).with_for_update()
                    )
                    if rental is not None and user is not None:
                        if ambiguous_result:
                            _mark_unknown_sms_rental(
                                rental,
                                reason=exc.code.lower(),
                                provider_balance_after=provider_balance_after,
                            )
                        else:
                            rental.last_error = exc.code[:255]
                            rental.provider_balance_after = provider_balance_after
                            await _refund_sms_rental(
                                session,
                                rental,
                                user,
                                reason=exc.code.lower(),
                                now=datetime.now(UTC),
                            )
                        current_balance = user.balance
                    else:
                        current_balance = balance_after_charge
            return SmsRentResult(
                False,
                "provider_result_unknown"
                if ambiguous_result
                else "provider_error_refunded"
                if provider_error_refunded
                else exc.code.lower(),
                rental_id=rental_id,
                shop_order_code=f"SMS{rental_id}",
                sale_amount=sale_amount,
                cost_amount=snapshot.unit_price,
                balance=current_balance,
                status="unknown" if ambiguous_result else "refunded",
                provider_balance_before=snapshot.balance,
                provider_balance_after=provider_balance_after,
            )

        async with session_factory() as session:
            async with session.begin():
                rental = await session.scalar(
                    select(SmsRental).where(SmsRental.id == rental_id).with_for_update()
                )
                user = await session.scalar(
                    select(User).where(User.telegram_id == user_id).with_for_update()
                )
                if rental is None or user is None:
                    return SmsRentResult(False, "storage_error")
                rental.provider_order_id = provider_rental.order_id
                rental.phone_number = provider_rental.phone_number
                rental.phone_number_display = provider_rental.phone_number_display
                rental.country_code = provider_rental.country_code
                rental.service_name = provider_rental.service_name or rental.service_name
                rental.status = provider_rental.status
                if provider_rental.status == "success":
                    await _complete_sms_success(
                        session,
                        rental,
                        user,
                        otp_code=provider_rental.otp_code,
                        otp_content=provider_rental.otp_content,
                        now=datetime.now(UTC),
                        referral_commission_percent=referral_commission_percent,
                    )
                await session.flush()
    return SmsRentResult(
                    True,
                    "success" if rental.status == "success" else "pending",
                    rental_id=rental.id,
                    shop_order_code=rental.shop_order_code,
                    phone_number=rental.phone_number or "",
                    phone_number_display=rental.phone_number_display or "",
                    sale_amount=rental.sale_amount,
                    cost_amount=rental.cost_amount,
                    balance=user.balance,
                    status=rental.status,
                    otp_code=rental.otp_code or "",
                    otp_content=rental.otp_content or "",
                )


async def attach_sms_waiting_message(
    session_factory: async_sessionmaker[AsyncSession],
    rental_id: int,
    user_id: int,
    message_id: int,
) -> bool:
    async with session_factory() as session:
        async with session.begin():
            rental = await session.scalar(
                select(SmsRental)
                .where(SmsRental.id == rental_id, SmsRental.user_id == user_id)
                .with_for_update()
            )
            if rental is None or rental.status != "pending":
                return False
            rental.waiting_message_id = message_id
            return True


async def attach_sms_rental_message(
    session_factory: async_sessionmaker[AsyncSession],
    rental_id: int,
    user_id: int,
    message_id: int,
) -> bool:
    async with session_factory() as session:
        async with session.begin():
            rental = await session.scalar(
                select(SmsRental)
                .where(SmsRental.id == rental_id, SmsRental.user_id == user_id)
                .with_for_update()
            )
            if rental is None or rental.status not in ACTIVE_SMS_STATUSES:
                return False
            rental.rental_message_id = message_id
            return True


async def refund_sms_rental(
    session_factory: async_sessionmaker[AsyncSession],
    rental_id: int,
    *,
    reason: str = "provider_order_refunded",
) -> SmsPollNotification | None:
    """Refund one provider order exactly once after its own status is verified."""
    async with session_factory() as session:
        async with session.begin():
            rental = await session.scalar(
                select(SmsRental).where(SmsRental.id == rental_id).with_for_update()
            )
            if rental is None or rental.status not in ACTIVE_SMS_STATUSES:
                return None
            user = await session.scalar(
                select(User).where(User.telegram_id == rental.user_id).with_for_update()
            )
            if user is None:
                return None
            await _refund_sms_rental(
                session,
                rental,
                user,
                reason=reason,
                now=datetime.now(UTC),
            )
            return SmsPollNotification(
                rental_id=rental.id,
                user_id=rental.user_id,
                rental_message_id=rental.rental_message_id,
                waiting_message_id=rental.waiting_message_id,
                status=rental.status,
                shop_order_code=rental.shop_order_code or f"SMS{rental.id}",
                phone_number=rental.phone_number or "",
                sale_amount=rental.sale_amount,
                balance=user.balance,
                language=user.language,
            )


async def recent_sms_rentals(
    session: AsyncSession,
    user_id: int,
    limit: int = 10,
) -> list[SmsRental]:
    return list(
        await session.scalars(
            select(SmsRental)
            .where(SmsRental.user_id == user_id)
            .order_by(SmsRental.id.desc())
            .limit(limit)
        )
    )


async def pending_sms_review_alerts(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    pending_alert_seconds: int = 900,
    limit: int = 50,
    now: datetime | None = None,
) -> list[SmsReviewAlert]:
    checked_at = now or datetime.now(UTC)
    pending_before = checked_at - timedelta(seconds=max(60, pending_alert_seconds))
    async with session_factory() as session:
        rentals = list(
            await session.scalars(
                select(SmsRental)
                .where(
                    SmsRental.review_alerted_at.is_(None),
                    or_(
                        SmsRental.status == "unknown",
                        and_(
                            SmsRental.status == "pending",
                            SmsRental.requested_at <= pending_before,
                        ),
                    ),
                )
                .order_by(SmsRental.id)
                .limit(limit)
            )
        )
    return [
        SmsReviewAlert(
            rental_id=rental.id,
            user_id=rental.user_id,
            status=rental.status,
            shop_order_code=rental.shop_order_code or f"SMS{rental.id}",
            phone_number=rental.phone_number or "",
            requested_at=_as_utc(rental.requested_at) or checked_at,
            provider_balance_before=rental.provider_balance_before,
            provider_balance_after=rental.provider_balance_after,
            last_error=rental.last_error or rental.failure_reason or "",
            poll_attempts=rental.poll_attempts,
        )
        for rental in rentals
    ]


async def mark_sms_review_alerted(
    session_factory: async_sessionmaker[AsyncSession],
    rental_id: int,
    *,
    now: datetime | None = None,
) -> bool:
    async with session_factory() as session:
        async with session.begin():
            rental = await session.scalar(
                select(SmsRental).where(SmsRental.id == rental_id).with_for_update()
            )
            if rental is None or rental.review_alerted_at is not None:
                return False
            rental.review_alerted_at = now or datetime.now(UTC)
            return True


async def poll_pending_sms_rentals(
    session_factory: async_sessionmaker[AsyncSession],
    client: RentSimClient,
    *,
    poll_seconds: int = 5,
    referral_commission_percent: int = 5,
    request_recovery_seconds: int = 120,
    limit: int = 50,
    now: datetime | None = None,
) -> list[SmsPollNotification]:
    checked_at = now or datetime.now(UTC)
    due_before = checked_at - timedelta(seconds=max(2, poll_seconds))
    stale_before = checked_at - timedelta(seconds=max(60, request_recovery_seconds))
    notifications: list[SmsPollNotification] = []

    # RentSim HTTP 500 means no rental order was created. Reconcile older rows
    # by their own provider result, never by the shared provider wallet delta.
    reconcile_before = checked_at - timedelta(seconds=max(30, poll_seconds * 2))
    async with session_factory() as session:
        refund_ids = list(
            await session.scalars(
                select(SmsRental.id)
                .where(
                    SmsRental.status == "unknown",
                    SmsRental.provider_order_id.is_(None),
                    SmsRental.phone_number.is_(None),
                    SmsRental.last_error == "provider_http_500",
                    SmsRental.requested_at <= reconcile_before,
                )
                .order_by(SmsRental.id)
                .limit(limit)
            )
        )
    for rental_id in refund_ids:
        async with session_factory() as session:
            async with session.begin():
                rental = await session.scalar(
                    select(SmsRental)
                    .where(
                        SmsRental.id == rental_id,
                        SmsRental.status == "unknown",
                        SmsRental.provider_order_id.is_(None),
                        SmsRental.phone_number.is_(None),
                        SmsRental.last_error == "provider_http_500",
                    )
                    .with_for_update()
                )
                if rental is None:
                    continue
                user = await session.scalar(
                    select(User).where(User.telegram_id == rental.user_id).with_for_update()
                )
                if user is None:
                    continue
                await _refund_sms_rental(
                    session,
                    rental,
                    user,
                    reason="provider_request_not_confirmed",
                    now=checked_at,
                )
                notifications.append(
                    SmsPollNotification(
                        rental_id=rental.id,
                        user_id=rental.user_id,
                        rental_message_id=rental.rental_message_id,
                        waiting_message_id=rental.waiting_message_id,
                        status=rental.status,
                        shop_order_code=rental.shop_order_code or f"SMS{rental.id}",
                        phone_number=rental.phone_number or "",
                        sale_amount=rental.sale_amount,
                        balance=user.balance,
                        language=user.language,
                        failure_reason=rental.failure_reason or "",
                    )
                )

    async with session_factory() as session:
        stale_ids = list(
            await session.scalars(
                select(SmsRental.id)
                .where(
                    SmsRental.status == "requesting",
                    SmsRental.requested_at <= stale_before,
                )
                .order_by(SmsRental.id)
                .limit(limit)
            )
        )
    for rental_id in stale_ids:
        async with session_factory() as session:
            async with session.begin():
                rental = await session.scalar(
                    select(SmsRental)
                    .where(
                        SmsRental.id == rental_id,
                        SmsRental.status == "requesting",
                    )
                    .with_for_update()
                )
                if rental is None:
                    continue
                user = await session.scalar(
                    select(User).where(User.telegram_id == rental.user_id).with_for_update()
                )
                if user is None:
                    continue
                _mark_unknown_sms_rental(
                    rental,
                    reason="stale_request_review",
                )
                notifications.append(
                    SmsPollNotification(
                        rental_id=rental.id,
                        user_id=rental.user_id,
                        rental_message_id=rental.rental_message_id,
                        waiting_message_id=rental.waiting_message_id,
                        status=rental.status,
                        shop_order_code=rental.shop_order_code or f"SMS{rental.id}",
                        phone_number=rental.phone_number or "",
                        sale_amount=rental.sale_amount,
                        balance=user.balance,
                        language=user.language,
                    )
                )
    async with session_factory() as session:
        rental_ids = list(
            await session.scalars(
                select(SmsRental.id)
                .where(
                    SmsRental.status == "pending",
                    or_(
                        SmsRental.last_checked_at.is_(None),
                        SmsRental.last_checked_at <= due_before,
                    ),
                )
                .order_by(SmsRental.id)
                .limit(limit)
            )
        )
    for rental_id in rental_ids:
        async with session_factory() as session:
            rental = await session.get(SmsRental, rental_id)
            provider_order_id = rental.provider_order_id if rental is not None else None
        if not provider_order_id:
            continue
        try:
            otp = await client.fetch_otp(provider_order_id)
        except RentSimError as exc:
            async with session_factory() as session:
                async with session.begin():
                    rental = await session.scalar(
                        select(SmsRental)
                        .where(SmsRental.id == rental_id, SmsRental.status == "pending")
                        .with_for_update()
                    )
                    if rental is not None:
                        rental.poll_attempts += 1
                        rental.last_checked_at = checked_at
                        rental.last_error = exc.code[:255]
            if exc.code == "INVALID_KEY":
                break
            continue

        async with session_factory() as session:
            async with session.begin():
                rental = await session.scalar(
                    select(SmsRental)
                    .where(SmsRental.id == rental_id, SmsRental.status == "pending")
                    .with_for_update()
                )
                if rental is None:
                    continue
                user = await session.scalar(
                    select(User).where(User.telegram_id == rental.user_id).with_for_update()
                )
                if user is None:
                    continue
                rental.poll_attempts += 1
                rental.last_checked_at = checked_at
                rental.last_error = None
                if otp.status == "pending":
                    continue
                if otp.status == "success":
                    await _complete_sms_success(
                        session,
                        rental,
                        user,
                        otp_code=otp.code,
                        otp_content=otp.content,
                        now=checked_at,
                        referral_commission_percent=referral_commission_percent,
                    )
                else:
                    await _refund_sms_rental(
                        session,
                        rental,
                        user,
                        reason="provider_failed" if otp.status == "failed" else "otp_timeout",
                        now=checked_at,
                    )
                notifications.append(
                    SmsPollNotification(
                        rental_id=rental.id,
                        user_id=rental.user_id,
                        rental_message_id=rental.rental_message_id,
                        waiting_message_id=rental.waiting_message_id,
                        status=rental.status,
                        shop_order_code=rental.shop_order_code or f"SMS{rental.id}",
                        phone_number=rental.phone_number or "",
                        sale_amount=rental.sale_amount,
                        balance=user.balance,
                        language=user.language,
                        otp_code=rental.otp_code or "",
                        otp_content=rental.otp_content or "",
                    )
                )
    return notifications
