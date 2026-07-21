import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ApiClient, ReferralReward, User
from app.utils import SecretCipher
from app.wallet_ledger import apply_wallet_change


def generate_referral_code() -> str:
    return f"REF{secrets.token_hex(4).upper()}"


async def ensure_referral_code(session: AsyncSession, user: User) -> str:
    if user.referral_code:
        return user.referral_code
    for _ in range(10):
        code = generate_referral_code()
        exists = await session.scalar(select(User.telegram_id).where(User.referral_code == code))
        if exists is None:
            user.referral_code = code
            await session.flush()
            return code
    raise RuntimeError("Could not allocate referral code")


def generate_api_credentials() -> tuple[str, str]:
    return f"VS{secrets.token_hex(8).upper()}", f"vs_live_{secrets.token_urlsafe(32)}"


async def ensure_api_client(
    session: AsyncSession,
    user_id: int,
    cipher: SecretCipher,
    default_rate_limit: int,
) -> tuple[ApiClient, str | None]:
    client = await session.scalar(select(ApiClient).where(ApiClient.owner_user_id == user_id))
    if client is not None:
        return client, None
    user = await session.scalar(select(User).where(User.telegram_id == user_id).with_for_update())
    if user is None:
        raise ValueError("User does not exist")
    client = await session.scalar(select(ApiClient).where(ApiClient.owner_user_id == user_id))
    if client is not None:
        return client, None
    api_id, secret = generate_api_credentials()
    while await session.scalar(select(ApiClient.id).where(ApiClient.api_id == api_id)) is not None:
        api_id, secret = generate_api_credentials()
    client = ApiClient(
        owner_user_id=user_id,
        api_id=api_id,
        encrypted_secret=cipher.encrypt(secret),
        rate_limit_per_minute=default_rate_limit,
    )
    session.add(client)
    await session.flush()
    return client, secret


async def rotate_api_secret(
    session: AsyncSession,
    user_id: int,
    cipher: SecretCipher,
) -> tuple[ApiClient, str]:
    client = await session.scalar(
        select(ApiClient).where(ApiClient.owner_user_id == user_id).with_for_update()
    )
    if client is None:
        raise ValueError("API client does not exist")
    _, secret = generate_api_credentials()
    client.encrypted_secret = cipher.encrypt(secret)
    client.secret_version += 1
    client.rotated_at = datetime.now(UTC)
    await session.flush()
    return client, secret


def api_signature(
    secret: str,
    timestamp: str,
    nonce: str,
    method: str,
    path: str,
    body: bytes,
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = "|".join(
        (timestamp, nonce, method.upper(), path, body_hash)
    ).encode()
    return hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class ReferralStats:
    invited_users: int
    rewarded_orders: int
    total_commission: int


async def referral_stats(session: AsyncSession, user_id: int) -> ReferralStats:
    invited_users = int(
        await session.scalar(select(func.count(User.telegram_id)).where(User.referred_by_id == user_id))
        or 0
    )
    rewarded_orders, total_commission = (
        await session.execute(
            select(
                func.count(ReferralReward.id),
                func.coalesce(func.sum(ReferralReward.commission_amount), 0),
            ).where(ReferralReward.referrer_user_id == user_id)
        )
    ).one()
    return ReferralStats(
        invited_users=invited_users,
        rewarded_orders=int(rewarded_orders),
        total_commission=int(total_commission),
    )


async def award_referral_commission(
    session: AsyncSession,
    buyer: User,
    *,
    shop_order_code: str,
    order_amount: int,
    sales_channel: str,
    commission_percent: int,
) -> ReferralReward | None:
    if buyer.referred_by_id is None or commission_percent <= 0 or order_amount <= 0:
        return None
    existing = await session.scalar(
        select(ReferralReward).where(ReferralReward.shop_order_code == shop_order_code)
    )
    if existing is not None:
        return existing
    commission = order_amount * commission_percent // 100
    if commission <= 0:
        return None
    referrer = await session.scalar(
        select(User).where(User.telegram_id == buyer.referred_by_id).with_for_update()
    )
    if referrer is None or referrer.telegram_id == buyer.telegram_id:
        return None
    apply_wallet_change(
        session,
        referrer,
        commission,
        kind="referral_commission",
        event_key=f"referral:{shop_order_code}",
        reference_type="referral",
        reference_id=shop_order_code,
        description=(
            f"Hoa hồng giới thiệu từ đơn {shop_order_code} của khách {buyer.telegram_id}"
        ),
    )
    reward = ReferralReward(
        referrer_user_id=referrer.telegram_id,
        referred_user_id=buyer.telegram_id,
        shop_order_code=shop_order_code,
        order_amount=order_amount,
        commission_amount=commission,
        sales_channel=sales_channel,
    )
    session.add(reward)
    await session.flush()
    return reward
