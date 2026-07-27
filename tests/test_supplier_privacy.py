from app.broadcasts import SaleAlertPayload, StockAlertPayload, sale_alert_text, stock_alert_text
from app.delivery import delivery_file, delivery_text
from app.public_api import public_order_failure_code
from app.sms_customer_messages import poll_notification_text
from app.utils import sanitize_customer_text


SUPPLIER_MARKERS = (
    "sumi",
    "sumistore",
    "lê hải",
    "le hai",
    "lehai",
    "rentsim",
    "rent sim",
    "sentsim",
    "provider_http",
    "supplier_http",
    "api.lehaipremium",
    "sumistore.me",
)


def assert_no_supplier_markers(text: str) -> None:
    lowered = text.casefold()
    for marker in SUPPLIER_MARKERS:
        assert marker.casefold() not in lowered


def test_customer_text_redacts_all_supplier_identities_and_source_markers() -> None:
    text = sanitize_customer_text(
        "SumiStore / Sumi / Lê Hải Premium / lehai / RentSim / Sentsim "
        "https://api.lehaipremium.me/api SP-GEF55PBV provider_http_500"
    )

    assert_no_supplier_markers(text)
    assert "nguồn hàng" in text
    assert "mã sản phẩm" in text
    assert "lỗi hệ thống" in text


def test_delivery_messages_redact_supplier_name_in_product_name() -> None:
    text = delivery_text(
        shop_order_code="B-1",
        product_name="GPT Plus - Lê Hải Premium",
        secrets=["account@example.com|password"],
        total_amount=40_000,
        language="vi",
    )
    document = delivery_file(
        shop_order_code="B-1",
        product_name="GPT Plus - Sumistore",
        secrets=["account@example.com|password"],
        total_amount=40_000,
        language="vi",
    )

    assert_no_supplier_markers(text)
    assert_no_supplier_markers(document.data.decode("utf-8-sig"))


def test_broadcast_messages_redact_supplier_name_in_product_name() -> None:
    sale = SaleAlertPayload(
        alert_id=1,
        product_id=2,
        name_vi="GPT Plus RentSim",
        name_en="GPT Plus Sentsim",
        old_price=40_000,
        new_price=35_000,
        stock=3,
        recipients=(),
    )
    stock = StockAlertPayload(
        alert_id=1,
        product_id=2,
        name_vi="GPT Plus Lê Hải",
        name_en="GPT Plus SumiStore",
        price=35_000,
        stock=3,
        recipients=(),
    )

    assert_no_supplier_markers(sale_alert_text(sale, "vi"))
    assert_no_supplier_markers(sale_alert_text(sale, "en"))
    assert_no_supplier_markers(stock_alert_text(stock, "vi"))
    assert_no_supplier_markers(stock_alert_text(stock, "en"))


def test_public_api_never_returns_internal_supplier_error_code() -> None:
    for code in (
        "SUPPLIER_HTTP_500",
        "SUPPLIER_UNAVAILABLE",
        "provider_http_500",
        "RentSim_timeout",
        "Sentsim_error",
    ):
        assert public_order_failure_code(code) == "ORDER_UNAVAILABLE"
    assert public_order_failure_code("OUT_OF_STOCK") == "OUT_OF_STOCK"


def test_otp_content_is_sanitized_before_customer_delivery() -> None:
    item = type(
        "Rental",
        (),
        {
            "status": "success",
            "language": "vi",
            "shop_order_code": "SMS1",
            "phone_number": "+85512345678",
            "otp_code": "123456",
            "otp_content": "RentSim provider_http_500",
        },
    )()

    assert_no_supplier_markers(poll_notification_text(item))
