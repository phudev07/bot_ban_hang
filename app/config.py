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

    sumistore_enabled: bool = False
    sumistore_base_url: str = "https://sumistore.me/api"
    sumistore_api_id: SecretStr = SecretStr("")
    sumistore_product_id: str = "SP-GEF55PBV"
    sumistore_product_ids_text: str = Field(
        default="",
        validation_alias="SUMISTORE_PRODUCT_IDS",
    )
    sumistore_markup: int = 5_000
    sumistore_fallback_price: int = 15_000
    sumistore_timeout_seconds: float = 15
    sumistore_sync_seconds: int = 60

    router_tokens_enabled: bool = False
    router_base_url: str = ""
    router_public_api_url: str = ""
    router_hmac_secret: SecretStr = SecretStr("")
    router_timeout_seconds: float = 15
    router_min_purchase: int = 10_000
    router_tokens_per_vnd: int = 1_000
    router_capacity_tokens: int = 0
    router_capacity_reserve_tokens: int = 10_000_000
    router_capacity_sync_seconds: int = 60
    router_allowed_models_text: str = Field(
        default="gpt-*,*/gpt-*",
        validation_alias="ROUTER_ALLOWED_MODELS",
    )

    inventory_encryption_key: SecretStr
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    public_base_url: str = ""
    token_usage_public_url: str = ""
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
        if self.router_tokens_enabled:
            if not self.router_base_url.startswith("https://"):
                raise ValueError("9Router internal URL must use HTTPS")
            if not self.router_public_api_url.startswith("https://"):
                raise ValueError("9Router public API URL must use HTTPS")
            if not self.router_hmac_secret.get_secret_value():
                raise ValueError("9Router HMAC secret is missing")
            if self.router_min_purchase < 10_000 or self.router_tokens_per_vnd <= 0:
                raise ValueError("9Router token rate configuration is invalid")
            if self.router_capacity_tokens < 0 or self.router_capacity_reserve_tokens < 0:
                raise ValueError("9Router capacity configuration is invalid")
            if self.router_capacity_sync_seconds < 15:
                raise ValueError("9Router capacity sync interval must be at least 15 seconds")
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

    @property
    def router_allowed_models(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                item.strip()
                for item in self.router_allowed_models_text.split(",")
                if item.strip()
            )
        )

    @property
    def token_usage_url(self) -> str:
        configured = self.token_usage_public_url.rstrip("/")
        if configured:
            return configured
        public_base_url = self.public_base_url.rstrip("/")
        return f"{public_base_url}/token-usage" if public_base_url else ""


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
