from types import SimpleNamespace

import pytest

from app.sms_customer_messages import (
    poll_notification_text,
    rental_failure_text,
    storefront_text,
)


BANNED_CUSTOMER_TERMS = (
    "rentsim",
    "provider_http",
    "provider unavailable",
    "provider connection",
    "nhà cung cấp",
    "key nguồn",
    "http://",
    "https://",
    "kh2",
)


def assert_upstream_details_hidden(text: str) -> None:
    lowered = text.casefold()
    for term in BANNED_CUSTOMER_TERMS:
        assert term.casefold() not in lowered


@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize(
    "message,status",
    [
        ("disabled", ""),
        ("out_of_stock", "refunded"),
        ("insufficient", ""),
        ("cooldown", ""),
        ("blocked", ""),
        ("invalid_key", "refunded"),
        ("provider_unavailable", "unknown"),
        ("provider_result_unknown", "unknown"),
        ("provider_error_refunded", "refunded"),
        ("https://rentsim.net/provider_http_500?server=kh2", "refunded"),
    ],
)
def test_sms_rental_failures_never_expose_upstream_details(
    language: str,
    message: str,
    status: str,
) -> None:
    text = rental_failure_text(
        language,
        message=message,
        status=status,
        sale_amount=2_000,
        balance=50_000,
        retry_after=60,
    )

    assert_upstream_details_hidden(text)


@pytest.mark.parametrize("language", ["vi", "en"])
def test_sms_storefront_uses_shop_neutral_status(language: str) -> None:
    text = storefront_text(
        language,
        connected=False,
        sale_price=2_000,
        effective_stock=0,
    )

    assert_upstream_details_hidden(text)


@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize(
    "status,failure_reason",
    [
        ("refunded", "provider_request_not_confirmed"),
        ("refunded", "provider_http_500"),
        ("unknown", "provider_unavailable"),
    ],
)
def test_sms_worker_notifications_hide_internal_failure_codes(
    language: str,
    status: str,
    failure_reason: str,
) -> None:
    item = SimpleNamespace(
        status=status,
        language=language,
        failure_reason=failure_reason,
        shop_order_code="SMS15",
        phone_number="+85512345678",
        sale_amount=2_000,
        balance=50_000,
        otp_code="",
        otp_content="",
    )

    text = poll_notification_text(item)

    assert_upstream_details_hidden(text)
    assert failure_reason.casefold() not in text.casefold()
