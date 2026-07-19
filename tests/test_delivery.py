from app.delivery import (
    delivery_file,
    delivery_keyboard,
    delivery_text,
)


def test_delivery_card_has_copy_buttons_and_txt_download() -> None:
    secrets = ["account1@example.com:password1", "account2@example.com:password2"]
    text = delivery_text(
        shop_order_code="BTEST123",
        product_name="ChatGPT Plus",
        secrets=secrets,
        total_amount=40_000,
        language="vi",
    )
    keyboard = delivery_keyboard(
        primary_order_id=11,
        secrets=secrets,
        language="vi",
    )

    assert "Tài khoản/code của bạn" in text
    assert "Mã đơn shop: <code>BTEST123</code>" in text
    assert "<pre>account1@example.com:password1\naccount2@example.com:password2</pre>" in text
    assert "1. account1@example.com:password1" not in text
    assert "2. account2@example.com:password2" not in text
    assert keyboard.inline_keyboard[0][0].copy_text.text == secrets[0]
    assert keyboard.inline_keyboard[1][0].copy_text.text == secrets[1]
    assert "#1" not in keyboard.inline_keyboard[0][0].text
    assert "#2" not in keyboard.inline_keyboard[1][0].text
    assert any(
        button.callback_data == "ordertxt:11"
        for row in keyboard.inline_keyboard
        for button in row
    )


def test_delivery_file_contains_all_accounts() -> None:
    document = delivery_file(
        shop_order_code="BFILE456",
        product_name="Tài khoản thử nghiệm",
        secrets=["first:secret", "second:secret"],
        total_amount=50_000,
        language="vi",
    )
    content = document.data.decode("utf-8-sig")

    assert document.filename == "don-hang-BFILE456.txt"
    assert "Mã đơn shop: BFILE456" in content
    assert "first:secret\nsecond:secret" in content
    assert "1. first:secret" not in content
    assert "2. second:secret" not in content
