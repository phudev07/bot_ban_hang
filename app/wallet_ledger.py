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
) -> WalletTransaction:
    """Apply a wallet mutation and record its exact before/after balance atomically."""
    signed_amount = int(amount)
    if signed_amount == 0:
        raise ValueError("Wallet transaction amount must be non-zero")
    balance_before = int(user.balance or 0)
    balance_after = balance_before + signed_amount
    if balance_after < 0:
        raise ValueError("Wallet balance cannot become negative")

    transaction = WalletTransaction(
        user_id=user.telegram_id,
        kind=kind[:32],
        amount=signed_amount,
        balance_before=balance_before,
        balance_after=balance_after,
        reference_type=reference_type[:32],
        reference_id=reference_id[:128],
        event_key=event_key[:191],
        description=description,
    )
    user.balance = balance_after
    session.add(transaction)
    return transaction
