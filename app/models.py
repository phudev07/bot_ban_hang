from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
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
    supplier_available_stock: Mapped[int] = mapped_column(default=0)
    supplier_available_stock_initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    supplier_owner_balance: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
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
    quantity_discounts: Mapped[list["QuantityDiscount"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class FlashSaleCampaign(Base):
    __tablename__ = "flash_sale_campaigns"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    original_price: Mapped[int] = mapped_column(BigInteger)
    sale_price: Mapped[int] = mapped_column(BigInteger)
    total_quantity: Mapped[int]
    sold_quantity: Mapped[int] = mapped_column(default=0, server_default="0")
    reserved_quantity: Mapped[int] = mapped_column(default=0, server_default="0")
    status: Mapped[str] = mapped_column(
        String(20), default="active", server_default="active", index=True
    )
    message_text: Mapped[str] = mapped_column(Text, default="")
    telegram_photo_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    notification_status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", index=True
    )
    total_recipients: Mapped[int] = mapped_column(default=0, server_default="0")
    delivered_count: Mapped[int] = mapped_column(default=0, server_default="0")
    failed_count: Mapped[int] = mapped_column(default=0, server_default="0")
    notification_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notification_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notification_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notification_last_error: Mapped[str | None] = mapped_column(
        String(500), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(255), default="admin")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    product: Mapped[Product] = relationship()


class ProductPriceAlert(Base):
    __tablename__ = "product_price_alerts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(24), index=True)
    supplier_price_before: Mapped[int] = mapped_column(BigInteger)
    supplier_price_after: Mapped[int] = mapped_column(BigInteger)
    sale_price_before: Mapped[int] = mapped_column(BigInteger)
    sale_price_after: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    total_recipients: Mapped[int] = mapped_column(default=0)
    delivered_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped[Product] = relationship()


class ProductStockAlert(Base):
    __tablename__ = "product_stock_alerts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(24), index=True)
    stock_before: Mapped[int] = mapped_column(default=0)
    stock_after: Mapped[int] = mapped_column(default=0)
    sale_price: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    total_recipients: Mapped[int] = mapped_column(default=0)
    delivered_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Keep the exact localized templates that were sent for audit/history.
    message_vi: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_en: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    product: Mapped[Product] = relationship()


class ProductAlertDelivery(Base):
    __tablename__ = "product_alert_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "alert_type",
            "alert_id",
            "user_id",
            name="uq_product_alert_delivery_recipient",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    alert_type: Mapped[str] = mapped_column(String(16), index=True)
    alert_id: Mapped[int] = mapped_column(index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    language: Mapped[str] = mapped_column(String(2), default="vi")
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", index=True
    )
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
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


class QuantityDiscount(Base):
    __tablename__ = "quantity_discounts"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "min_quantity",
            name="uq_quantity_discount_product_threshold",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    min_quantity: Mapped[int]
    discount_percent: Mapped[int]
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    product: Mapped[Product] = relationship(back_populates="quantity_discounts")


class InventoryItem(Base):
    __tablename__ = "inventory_items"
    __table_args__ = (
        UniqueConstraint(
            "supplier_order_code",
            "supplier_item_index",
            name="uq_inventory_supplier_source",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    encrypted_secret: Mapped[str] = mapped_column(Text)
    cost_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    supplier_order_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    supplier_item_index: Mapped[int | None] = mapped_column(nullable=True)
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
    flash_sale_id: Mapped[int | None] = mapped_column(
        ForeignKey("flash_sale_campaigns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
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
    flash_sale_id: Mapped[int | None] = mapped_column(
        ForeignKey("flash_sale_campaigns.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    flash_sale_quantity: Mapped[int] = mapped_column(default=0, server_default="0")
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


class SmsRental(Base):
    __tablename__ = "sms_rentals"
    __table_args__ = (
        Index("ix_sms_rentals_status_last_checked", "status", "last_checked_at", "id"),
        Index("ix_sms_rentals_status_requested", "status", "requested_at", "id"),
        Index("ix_sms_rentals_user_requested", "user_id", "requested_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    shop_order_code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, unique=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(24), default="rentsim", index=True)
    provider_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    service_id: Mapped[str] = mapped_column(String(64), default="chatgpt", index=True)
    service_name: Mapped[str] = mapped_column(String(128), default="ChatGPT")
    server_id: Mapped[str] = mapped_column(String(32), default="kh2", index=True)
    phone_number: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    phone_number_display: Mapped[str | None] = mapped_column(String(64), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="requesting", index=True)
    sale_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_amount: Mapped[int] = mapped_column(BigInteger, default=0)
    provider_balance_before: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    provider_balance_after: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_stock: Mapped[int] = mapped_column(default=0)
    otp_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    otp_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    rental_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    waiting_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    poll_attempts: Mapped[int] = mapped_column(default=0)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_alerted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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


class SupplierRecoveryRequest(Base):
    __tablename__ = "supplier_recovery_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), default="sumistore", index=True)
    request_key: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    supplier_product_id: Mapped[str] = mapped_column(String(64), index=True)
    quantity: Mapped[int]
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    supplier_order_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    unit_price: Mapped[int] = mapped_column(BigInteger, default=0)
    total_cost: Mapped[int] = mapped_column(BigInteger, default=0)
    inserted_count: Mapped[int] = mapped_column(default=0)
    audit_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier_balance_transactions.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    supplier_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    recovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SupplierPurchaseAttempt(Base):
    __tablename__ = "supplier_purchase_attempts"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "request_key",
            name="uq_supplier_purchase_attempt_request",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    request_key: Mapped[str] = mapped_column(String(128), index=True)
    product_id: Mapped[int | None] = mapped_column(
        ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    supplier_product_id: Mapped[str] = mapped_column(String(64), index=True)
    quantity: Mapped[int]
    status: Mapped[str] = mapped_column(String(24), default="processing", index=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    supplier_order_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TutorialMedia(Base):
    __tablename__ = "tutorial_media"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    telegram_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class BroadcastLog(Base):
    __tablename__ = "broadcast_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    source_message_id: Mapped[int]
    total_recipients: Mapped[int] = mapped_column(default=0)
    delivered_count: Mapped[int] = mapped_column(default=0)
    failed_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(
        String(20), default="queued", server_default="queued", index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BroadcastDelivery(Base):
    __tablename__ = "broadcast_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "broadcast_id",
            "user_id",
            name="uq_broadcast_delivery_recipient",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    broadcast_id: Mapped[int] = mapped_column(
        ForeignKey("broadcast_logs.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.telegram_id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending", index=True
    )
    attempt_count: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
