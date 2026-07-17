from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str] = mapped_column(String(2), default="vi")
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    has_started: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    referral_code: Mapped[str | None] = mapped_column(String(24), nullable=True, unique=True)
    referred_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_vi: Mapped[str] = mapped_column(String(255))
    name_en: Mapped[str] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"))
    name_vi: Mapped[str] = mapped_column(String(255))
    name_en: Mapped[str] = mapped_column(String(255))
    description_vi: Mapped[str] = mapped_column(Text, default="")
    description_en: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[int] = mapped_column(BigInteger)
    product_type: Mapped[str] = mapped_column(String(20), default="account", index=True)
    allow_quantity: Mapped[bool] = mapped_column(Boolean, default=False)
    max_quantity: Mapped[int] = mapped_column(default=10)
    fulfillment_source: Mapped[str] = mapped_column(String(20), default="local", index=True)
    supplier_product_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    supplier_markup: Mapped[int] = mapped_column(BigInteger, default=0)
    supplier_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    external_stock: Mapped[int] = mapped_column(default=0)
    supplier_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    category: Mapped[Category] = relationship(back_populates="products")
    inventory: Mapped[list["InventoryItem"]] = relationship(back_populates="product")
    discount_codes: Mapped[list["DiscountCode"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class DiscountCode(Base):
    __tablename__ = "discount_codes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    discount_type: Mapped[str] = mapped_column(String(20), default="fixed")
    discount_value: Mapped[int] = mapped_column(BigInteger)
    max_uses: Mapped[int] = mapped_column(default=0)
    used_count: Mapped[int] = mapped_column(default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    product: Mapped[Product] = relationship(back_populates="discount_codes")


class InventoryItem(Base):
    __tablename__ = "inventory_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    encrypted_secret: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="available", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped[Product] = relationship(back_populates="inventory")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    inventory_item_id: Mapped[int] = mapped_column(ForeignKey("inventory_items.id"), unique=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    cost_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    discount_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    discount_code_id: Mapped[int | None] = mapped_column(
        ForeignKey("discount_codes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    discount_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    batch_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    supplier_order_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sales_channel: Mapped[str] = mapped_column(String(16), default="telegram", index=True)
    api_client_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    api_order_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_order_requests.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped[Product] = relationship()
    inventory_item: Mapped[InventoryItem] = relationship()

    @property
    def shop_order_code(self) -> str:
        return self.batch_code or f"O{self.id}"


class Deposit(Base):
    __tablename__ = "deposits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    requested_amount: Mapped[int] = mapped_column(BigInteger)
    paid_amount: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payment_kind: Mapped[str] = mapped_column(String(20), default="wallet", index=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id"), nullable=True, index=True
    )
    quantity: Mapped[int] = mapped_column(default=1)
    discount_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    discount_code_id: Mapped[int | None] = mapped_column(
        ForeignKey("discount_codes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    discount_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    telegram_message_ids: Mapped[str] = mapped_column(Text, default="", server_default="")
    messages_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    deposit_id: Mapped[int] = mapped_column(ForeignKey("deposits.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    provider_tx_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    credit_status: Mapped[str] = mapped_column(
        String(32), default="credited", server_default="credited", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BalanceAdjustment(Base):
    __tablename__ = "balance_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.telegram_id"), index=True)
    admin_username: Mapped[str] = mapped_column(String(255))
    amount: Mapped[int] = mapped_column(BigInteger)
    reason: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiClient(Base):
    __tablename__ = "api_clients"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    owner_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), unique=True, index=True
    )
    api_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    encrypted_secret: Mapped[str] = mapped_column(Text)
    secret_version: Mapped[int] = mapped_column(default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    admin_blocked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    rate_limit_per_minute: Mapped[int] = mapped_column(default=60)
    allowed_ips: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiOrderRequest(Base):
    __tablename__ = "api_order_requests"
    __table_args__ = (
        UniqueConstraint("api_client_id", "idempotency_key", name="uq_api_order_idempotency"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    api_client_id: Mapped[int] = mapped_column(
        ForeignKey("api_clients.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="processing", index=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    shop_order_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ApiRequestAudit(Base):
    __tablename__ = "api_request_audits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    api_client_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_clients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(255))
    status_code: Mapped[int]
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    duration_ms: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class ReferralReward(Base):
    __tablename__ = "referral_rewards"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    referrer_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    referred_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    shop_order_code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    order_amount: Mapped[int] = mapped_column(BigInteger)
    commission_amount: Mapped[int] = mapped_column(BigInteger)
    sales_channel: Mapped[str] = mapped_column(String(16), default="telegram", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class SupplierBalanceState(Base):
    __tablename__ = "supplier_balance_states"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_balance: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_purchase_id: Mapped[int] = mapped_column(BigInteger, default=0)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SupplierBalanceTransaction(Base):
    __tablename__ = "supplier_balance_transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), default="sumistore", index=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    balance_before: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    balance_after: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    supplier_order_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    shop_order_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    quantity: Mapped[int] = mapped_column(default=0)
    note: Mapped[str] = mapped_column(String(500), default="")
    period_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BroadcastLog(Base):
    __tablename__ = "broadcast_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int]
    total_recipients: Mapped[int] = mapped_column(default=0)
    delivered_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
