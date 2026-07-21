import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.config import Settings


def base_settings(**overrides):
    values = {
        "_env_file": None,
        "bot_token": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        "inventory_encryption_key": Fernet.generate_key().decode(),
        "sepay_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_bot_can_start_with_sepay_disabled() -> None:
    settings = base_settings()
    assert settings.sepay_enabled is False
    assert settings.database_pool_size == 10
    assert settings.database_max_overflow == 10
    assert settings.database_pool_timeout_seconds == 8


def test_database_pool_configuration_is_bounded() -> None:
    with pytest.raises(ValidationError):
        base_settings(database_pool_size=0)
    with pytest.raises(ValidationError):
        base_settings(database_max_overflow=-1)
    with pytest.raises(ValidationError):
        base_settings(database_pool_timeout_seconds=0.5)


def test_enabled_sepay_requires_bank_configuration() -> None:
    with pytest.raises(ValidationError):
        base_settings(sepay_enabled=True)


def test_hmac_sepay_configuration() -> None:
    settings = base_settings(
        sepay_enabled=True,
        sepay_auth_mode="hmac",
        sepay_webhook_secret="secret",
        bank_code="MBBank",
        bank_account="123456789",
        bank_account_name="PHAM HAI PHU",
        payment_prefix="nap",
    )
    assert settings.payment_prefix == "NAP"
    assert settings.payment_expiry_seconds == 300
    assert settings.payment_expiry_sweep_seconds == 2


def test_payment_expiry_configuration_is_bounded() -> None:
    with pytest.raises(ValidationError):
        base_settings(payment_expiry_seconds=59)
    with pytest.raises(ValidationError):
        base_settings(payment_expiry_sweep_seconds=0)


def test_spam_protection_configuration_is_bounded() -> None:
    settings = base_settings()
    assert settings.bot_spam_protection_enabled is True
    assert settings.max_pending_deposits_per_user == 3
    assert settings.sepay_webhook_rate_limit_per_minute == 60

    with pytest.raises(ValidationError):
        base_settings(bot_burst_rate_limit=1)
    with pytest.raises(ValidationError):
        base_settings(max_pending_deposits_per_user=0)
    with pytest.raises(ValidationError):
        base_settings(
            sepay_webhook_rate_limit_per_minute=100,
            sepay_webhook_global_rate_limit_per_minute=50,
        )


def test_enabled_sumistore_requires_api_id() -> None:
    with pytest.raises(ValidationError):
        base_settings(sumistore_enabled=True)

    settings = base_settings(
        sumistore_enabled=True,
        sumistore_api_id="TAPI-test-only",
    )
    assert settings.sumistore_markup == 5_000
    assert settings.supplier_ui_cache_seconds == 10

    with pytest.raises(ValidationError):
        base_settings(supplier_ui_cache_seconds=0)
    with pytest.raises(ValidationError):
        base_settings(supplier_ui_cache_seconds=61)


def test_sumistore_supports_multiple_product_ids() -> None:
    settings = base_settings(
        SUMISTORE_PRODUCT_IDS="SP-GEF55PBV, SP-JMYJL2PL,SP-GEF55PBV",
    )
    assert settings.sumistore_product_ids == ("SP-GEF55PBV", "SP-JMYJL2PL")


def test_enabled_lehai_requires_buyer_key() -> None:
    with pytest.raises(ValidationError):
        base_settings(lehai_enabled=True)

    settings = base_settings(
        lehai_enabled=True,
        lehai_api_key="tgb_test-only",
        LEHAI_PRODUCT_IDS="cdk_pixel, cdk_ggpro_18m,cdk_pixel",
    )
    assert settings.lehai_product_ids == ("cdk_pixel", "cdk_ggpro_18m")
    assert settings.lehai_markup == 5_000


def test_enabled_rentsim_requires_key_and_uses_cambodia_chatgpt_defaults() -> None:
    with pytest.raises(ValidationError):
        base_settings(rentsim_enabled=True)

    settings = base_settings(
        rentsim_enabled=True,
        rentsim_api_key="secret-test",
    )
    assert settings.rentsim_server_id == "kh2"
    assert settings.rentsim_service_id == "chatgpt"
    assert settings.rentsim_markup == 1_000
    assert settings.rentsim_fallback_price == 1_000
    assert settings.rentsim_cooldown_seconds == 60
    assert settings.rentsim_request_recovery_seconds == 120
