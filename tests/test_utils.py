import hashlib
import hmac

from cryptography.fernet import Fernet

from app.utils import (
    SecretCipher,
    build_sepay_qr_url,
    find_deposit_code,
    format_vnd,
    inventory_account_identity,
    normalize_inventory_identity,
    parse_vnd,
    round_vnd_to_thousand,
    safe_customer_telegram_html,
    verify_sepay_hmac,
)


def test_format_and_parse_vnd() -> None:
    assert format_vnd(1234567) == "1.234.567đ"
    assert parse_vnd("100.000 đ") == 100_000
    assert parse_vnd("abc") is None


def test_round_vnd_to_thousand_preserves_exact_five_hundred() -> None:
    assert round_vnd_to_thousand(21_375) == 21_000
    assert round_vnd_to_thousand(21_499) == 21_000
    assert round_vnd_to_thousand(21_500) == 21_500
    assert round_vnd_to_thousand(21_501) == 22_000
    assert round_vnd_to_thousand(21_999) == 22_000


def test_find_deposit_code() -> None:
    assert find_deposit_code("Thanh toan nap123456789abcd") == "NAP123456789ABCD"
    assert find_deposit_code("DH123456789ABCD", "DH") == "DH123456789ABCD"
    assert (
        find_deposit_code("NAP6799701918 NAP67997019184177 BankAPINotify NAP67997019184177")
        == "NAP67997019184177"
    )
    assert find_deposit_code("khong co ma") is None


def test_qr_url_is_encoded() -> None:
    url = build_sepay_qr_url("MB", "0123", 100_000, "NAP123456ABCD")
    assert url.startswith("https://qr.sepay.vn/img?")
    assert "amount=100000" in url
    assert "des=NAP123456ABCD" in url


def test_secret_cipher_round_trip() -> None:
    cipher = SecretCipher(Fernet.generate_key().decode())
    encrypted = cipher.encrypt("user:password")
    assert "user:password" not in encrypted
    assert cipher.decrypt(encrypted) == "user:password"


def test_inventory_identity_and_fingerprint_ignore_password_and_case() -> None:
    cipher = SecretCipher(Fernet.generate_key().decode())
    assert inventory_account_identity("Email: User@Example.com\nPassword: secret") == (
        "User@Example.com"
    )
    assert normalize_inventory_identity(" USER@example.com | first-password ") == (
        "user@example.com"
    )
    assert cipher.inventory_fingerprint("User@Example.com|first-password") == (
        cipher.inventory_fingerprint("user@example.com|different-password")
    )
    assert cipher.inventory_fingerprint("other@example.com|first-password") != (
        cipher.inventory_fingerprint("user@example.com|first-password")
    )


def test_inventory_identity_ignores_supplier_contact_instructions() -> None:
    cipher = SecretCipher(Fernet.generate_key().decode())

    assert inventory_account_identity("Liên hệ @seller có hàng ngay sau 1p") == ""
    assert inventory_account_identity("contact @seller for delivery") == ""
    assert cipher.inventory_fingerprint("Liên hệ @seller có hàng ngay sau 1p") is None


def test_product_description_keeps_safe_telegram_formatting_and_emoji() -> None:
    rendered = safe_customer_telegram_html(
        '<strong>Sale 🔥</strong> <em>hôm nay</em> '
        '<a href="https://example.com/offer?a=1&b=2">Xem ngay</a>'
    )

    assert rendered == (
        '<b>Sale 🔥</b> <i>hôm nay</i> '
        '<a href="https://example.com/offer?a=1&amp;b=2">Xem ngay</a>'
    )


def test_product_description_removes_unsafe_html_and_supplier_identity() -> None:
    rendered = safe_customer_telegram_html(
        '<script>alert(1)</script><b>SumiStore</b> '
        '<a href="javascript:alert(1)" onclick="bad()">bấm vào</a> '
        '<a href="https://api.lehaipremium.me/order">nguồn</a>'
    )

    assert "<script" not in rendered
    assert "javascript:" not in rendered
    assert "onclick" not in rendered
    assert "sumistore" not in rendered.casefold()
    assert "lehaipremium" not in rendered.casefold()
    assert "<b>nguồn hàng</b>" in rendered
    assert "bấm vào" in rendered


def test_product_description_balances_malformed_telegram_html() -> None:
    assert safe_customer_telegram_html("<b>đậm <i>nghiêng</b> thường") == (
        "<b>đậm <i>nghiêng</i></b> thường"
    )


def test_product_description_keeps_valid_telegram_custom_emoji_only() -> None:
    rendered = safe_customer_telegram_html(
        '<tg-emoji emoji-id="5312241539987020022">🔥</tg-emoji> '
        '<tg-emoji emoji-id="invalid" onclick="bad()">⚡</tg-emoji>'
    )

    assert rendered == (
        '<tg-emoji emoji-id="5312241539987020022">🔥</tg-emoji> ⚡'
    )
    assert "onclick" not in rendered


def test_verify_sepay_hmac() -> None:
    body = b'{"id":92704,"transferType":"in"}'
    timestamp = "1700000000"
    secret = "test-secret"
    signature = (
        "sha256="
        + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    )

    assert verify_sepay_hmac(body, signature, timestamp, secret, now=1700000000)
    assert not verify_sepay_hmac(body + b" ", signature, timestamp, secret, now=1700000000)
    assert not verify_sepay_hmac(body, signature, timestamp, secret, now=1700000601)
