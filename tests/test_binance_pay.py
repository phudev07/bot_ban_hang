from app.binance_pay import (
    binance_pay_signature,
    usdt_to_vnd,
    vnd_to_usdt,
    verify_binance_pay_signature,
)


def test_binance_pay_vnd_usdt_conversion_is_decimal_and_reversible() -> None:
    assert vnd_to_usdt(27_500) == vnd_to_usdt(27_500, 27_500)
    assert str(vnd_to_usdt(27_500)) == "1.00000000"
    assert usdt_to_vnd("0.4", 27_500) == 11_000


def test_binance_pay_webhook_signature_checks_timestamp_and_body() -> None:
    body = b'{"bizStatus":"PAY_SUCCESS"}'
    secret = "secret"
    timestamp = "1700000000000"
    nonce = "nonce"
    signature = binance_pay_signature(body, timestamp, nonce, secret)

    assert verify_binance_pay_signature(
        body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        secret_key=secret,
        now=1_700_000_000,
    )
    assert not verify_binance_pay_signature(
        body + b"x",
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        secret_key=secret,
        now=1_700_000_000,
    )
