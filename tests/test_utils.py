import hashlib
import hmac

from cryptography.fernet import Fernet

from app.utils import (
    SecretCipher,
    build_sepay_qr_url,
    find_deposit_code,
    format_vnd,
    parse_vnd,
    verify_sepay_hmac,
)


def test_format_and_parse_vnd() -> None:
    assert format_vnd(1234567) == "1.234.567đ"
    assert parse_vnd("100.000 đ") == 100_000
    assert parse_vnd("abc") is None


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
