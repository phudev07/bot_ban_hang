from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    database_url: str = "postgresql+asyncpg://shop:change_me@postgres:5432/shop"
    redis_url: str = "redis://redis:6379/0"
    admin_ids_text: str = Field(default="", validation_alias="ADMIN_IDS")
    deposit_notification_bot_token: SecretStr = SecretStr("")

    support_username: str = "phptoolvip"
    community_group_url: str = "https://t.me/groupphptool"

    sepay_enabled: bool = False
    sepay_auth_mode: Literal["hmac", "api_key"] = "hmac"
    sepay_webhook_secret: SecretStr = SecretStr("")
    sepay_api_key: SecretStr = SecretStr("")
    bank_code: str = ""
    bank_account: str = ""
    bank_account_name: str = ""
    payment_prefix: str = "NAP"
    min_deposit: int = 10_000
    payment_expiry_seconds: int = 300
    payment_expiry_sweep_seconds: int = 2
    max_pending_deposits_per_user: int = 3

    bot_spam_protection_enabled: bool = True
    bot_global_rate_limit_per_minute: int = 45
    bot_burst_rate_limit: int = 8
    bot_deposit_rate_limit_per_5_minutes: int = 4
    bot_purchase_rate_limit_per_minute: int = 10
    sepay_webhook_rate_limit_per_minute: int = 60
    sepay_webhook_global_rate_limit_per_minute: int = 300
    public_api_ip_rate_limit_per_minute: int = 180
    public_api_global_rate_limit_per_minute: int = 1_500

    sumistore_enabled: bool = False
    sumistore_base_url: str = "https://sumistore.me/api"
    sumistore_api_id: SecretStr = SecretStr("")
    sumistore_product_id: str = "SP-GEF55PBV"
    sumistore_product_ids_text: str = Field(
        default="SP-GEF55PBV,SP-JMYJL2PL",
        validation_alias="SUMISTORE_PRODUCT_IDS",
    )
    sumistore_markup: int = 5_000
    sumistore_fallback_price: int = 15_000
    sumistore_timeout_seconds: float = 15
    sumistore_sync_seconds: int = 60
    sumistore_audit_seconds: int = 30

    shop_api_enabled: bool = True
    shop_api_base_url: str = "https://token.vietshare.site/v1"
    shop_api_rate_limit_per_minute: int = 60
    shop_api_signature_tolerance_seconds: int = 300
    referral_commission_percent: int = 5

    inventory_encryption_key: SecretStr
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    public_base_url: str = ""
    dashboard_enabled: bool = False
    dashboard_username: str = "admin"
    dashboard_password_hash: SecretStr = SecretStr("")
    dashboard_session_secret: SecretStr = SecretStr("")
    seed_demo_data: bool = True
    log_level: str = "INFO"

    @field_validator("support_username", mode="before")
    @classmethod
    def strip_at_sign(cls, value: object) -> object:
        return value.lstrip("@") if isinstance(value, str) else value

    @field_validator("payment_prefix", mode="before")
    @classmethod
    def normalize_payment_prefix(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        prefix = value.strip().upper()
        if not 2 <= len(prefix) <= 10 or not prefix.isalnum():
            raise ValueError("Payment prefix must contain 2-10 letters or numbers")
        return prefix

    @model_validator(mode="after")
    def validate_sepay_configuration(self) -> "Settings":
        if self.sepay_enabled:
            if not all((self.bank_code, self.bank_account, self.bank_account_name)):
                raise ValueError("SePay is enabled but bank details are missing")
            if self.sepay_auth_mode == "hmac":
                if not self.sepay_webhook_secret.get_secret_value():
                    raise ValueError("SePay HMAC secret is missing")
            elif not self.sepay_api_key.get_secret_value():
                raise ValueError("SePay API key is missing")
        if self.dashboard_enabled and not all(
            (
                self.dashboard_username,
                self.dashboard_password_hash.get_secret_value(),
                self.dashboard_session_secret.get_secret_value(),
            )
        ):
            raise ValueError("Dashboard credentials or session secret are missing")
        if self.sumistore_enabled and not self.sumistore_api_id.get_secret_value():
            raise ValueError("Sumistore is enabled but API ID is missing")
        if self.sumistore_markup < 0 or self.sumistore_fallback_price <= 0:
            raise ValueError("Sumistore price configuration is invalid")
        if self.sumistore_audit_seconds < 10:
            raise ValueError("Sumistore audit interval must be at least 10 seconds")
        if self.shop_api_rate_limit_per_minute < 1:
            raise ValueError("Shop API rate limit must be positive")
        if self.shop_api_signature_tolerance_seconds < 30:
            raise ValueError("Shop API signature tolerance is too small")
        if not 60 <= self.payment_expiry_seconds <= 3600:
            raise ValueError("Payment expiry must be between 60 and 3600 seconds")
        if not 1 <= self.payment_expiry_sweep_seconds <= 60:
            raise ValueError("Payment expiry sweep must be between 1 and 60 seconds")
        if not 1 <= self.max_pending_deposits_per_user <= 20:
            raise ValueError("Pending deposit limit must be between 1 and 20")
        if self.bot_global_rate_limit_per_minute < 5 or self.bot_burst_rate_limit < 2:
            raise ValueError("Bot rate limits are too small")
        if self.bot_deposit_rate_limit_per_5_minutes < 1:
            raise ValueError("Bot deposit rate limit must be positive")
        if self.bot_purchase_rate_limit_per_minute < 1:
            raise ValueError("Bot purchase rate limit must be positive")
        if self.sepay_webhook_rate_limit_per_minute < 10:
            raise ValueError("SePay webhook rate limit is too small")
        if (
            self.sepay_webhook_global_rate_limit_per_minute
            < self.sepay_webhook_rate_limit_per_minute
        ):
            raise ValueError("Global SePay webhook limit must cover the per-IP limit")
        if self.public_api_ip_rate_limit_per_minute < 10:
            raise ValueError("Public API IP rate limit is too small")
        if (
            self.public_api_global_rate_limit_per_minute
            < self.public_api_ip_rate_limit_per_minute
        ):
            raise ValueError("Global public API limit must cover the per-IP limit")
        if not 0 <= self.referral_commission_percent <= 100:
            raise ValueError("Referral commission percent must be between 0 and 100")
        return self

    @property
    def admin_ids(self) -> tuple[int, ...]:
        return tuple(int(item.strip()) for item in self.admin_ids_text.split(",") if item.strip())

    @property
    def sumistore_product_ids(self) -> tuple[str, ...]:
        configured = [
            item.strip()
            for item in self.sumistore_product_ids_text.split(",")
            if item.strip()
        ]
        values = configured or [self.sumistore_product_id]
        return tuple(dict.fromkeys(values))

@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
