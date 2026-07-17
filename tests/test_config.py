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


def test_enabled_sumistore_requires_api_id() -> None:
    with pytest.raises(ValidationError):
        base_settings(sumistore_enabled=True)

    settings = base_settings(
        sumistore_enabled=True,
        sumistore_api_id="TAPI-test-only",
    )
    assert settings.sumistore_markup == 5_000


def test_sumistore_supports_multiple_product_ids() -> None:
    settings = base_settings(
        SUMISTORE_PRODUCT_IDS="SP-GEF55PBV, SP-JMYJL2PL,SP-GEF55PBV",
    )
    assert settings.sumistore_product_ids == ("SP-GEF55PBV", "SP-JMYJL2PL")
