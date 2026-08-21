from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, WalletTransaction


def apply_wallet_change(
    session: AsyncSession,
    user: User,
    amount: int,
    *,
    kind: str,
    event_key: str,
    reference_type: str,
    reference_id: str,
    description: str,
    currency: str = "VND",
) -> WalletTransaction:
    """Apply a VND or USD wallet mutation atomically.

    USD amounts use tenths (10 = $1.0), keeping the two wallet currencies
    completely independent and avoiding floating-point arithmetic.
    """
    currency = str(currency or "VND").upper()
    if currency not in {"VND", "USD"}:
        raise ValueError("Unsupported wallet currency")
    signed_amount = int(amount)
    if signed_amount == 0:
        raise ValueError("Wallet transaction amount must be non-zero")
    balance_attr = "balance_usd_tenths" if currency == "USD" else "balance"
    balance_before = int(getattr(user, balance_attr, 0) or 0)
    balance_after = balance_before + signed_amount
    if balance_after < 0:
        raise ValueError("Wallet balance cannot become negative")

    transaction = WalletTransaction(
        user_id=user.telegram_id,
        kind=kind[:32],
        amount=signed_amount,
        balance_before=balance_before,
        balance_after=balance_after,
        currency=currency,
        reference_type=reference_type[:32],
        reference_id=reference_id[:128],
        event_key=event_key[:191],
        description=description,
    )
    setattr(user, balance_attr, balance_after)
    session.add(transaction)
    return transaction
