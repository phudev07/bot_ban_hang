from app.delivery import (
    delivery_file,
    delivery_files,
    delivery_keyboard,
    delivery_text,
)


def test_delivery_card_has_only_copy_all_and_txt_download() -> None:
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
    assert "🧾 Mã đơn shop:" in text
    assert "🧮 Số lượng: <b>2</b>" in text
    assert "💰 Tổng tiền: <b>40.000đ</b>" in text
    assert "📋 <b>Tài khoản/code của bạn:</b>" in text
    assert "<pre>account1@example.com:password1\naccount2@example.com:password2</pre>" in text
    assert "1. account1@example.com:password1" not in text
    assert "2. account2@example.com:password2" not in text
    copy_buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.copy_text is not None
    ]
    assert len(copy_buttons) == 1
    assert copy_buttons[0].text == "📋 Sao chép tất cả"
    assert copy_buttons[0].copy_text.text == "\n".join(secrets)
    assert any(
        button.callback_data == "ordertxt:11"
        for row in keyboard.inline_keyboard
        for button in row
    )


def test_long_delivery_uses_txt_without_creating_many_copy_buttons() -> None:
    secrets = [f"account-{index}-" + ("x" * 80) for index in range(5)]
    keyboard = delivery_keyboard(
        primary_order_id=12,
        secrets=secrets,
        language="vi",
    )

    assert not any(
        button.copy_text is not None
        for row in keyboard.inline_keyboard
        for button in row
    )
    assert keyboard.inline_keyboard[0][0].callback_data == "ordertxt:12"


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


def test_gpt_free_delivery_creates_account_txt_and_full_json() -> None:
    secrets = [
        'email1@gmail.com|Matkhau1|2FASECRET1|{"accessToken":"token-1","refreshToken":"refresh-1","email":"email1@gmail.com"}',
        'email2@gmail.com|Matkhau2|2FASECRET2|{"accessToken":"token-2","refreshToken":"refresh-2","email":"email2@gmail.com"}',
    ]
    documents = delivery_files(
        shop_order_code="BGPTFREE1",
        product_name="GPT FREE",
        secrets=secrets,
        total_amount=40_000,
        language="vi",
        product_id=28,
    )

    assert [document.filename for document in documents] == [
        "don-hang-BGPTFREE1-acc.txt",
        "don-hang-BGPTFREE1-full.json",
    ]
    account_content = documents[0].data.decode("utf-8-sig")
    full_content = documents[1].data.decode("utf-8-sig")
    assert "email1@gmail.com|Matkhau1|2FASECRET1" in account_content
    assert "email1@gmail.com|Matkhau1|2FASECRET1|{" not in account_content
    assert secrets[1] in full_content


def test_gpt_free_delivery_message_does_not_include_large_token_payload() -> None:
    secret = (
        "email@example.com|password|2FASECRET|"
        + '{"accessToken":"token-"' + ("x" * 3_000) + "}"
    )
    text = delivery_text(
        shop_order_code="BGPTFREE2",
        product_name="GPT FREE",
        secrets=[secret, secret],
        total_amount=10_000,
        language="vi",
    )

    assert len(text) < 4_096
    assert "accessToken" not in text
    assert "email@example.com|password|2FASECRET" in text


def test_codex_delivery_can_open_setup_guide() -> None:
    text = delivery_text(
        shop_order_code="BCODEX13",
        product_name="API Codex 10M Token · 24 giờ",
        secrets=["key-codex-test"],
        total_amount=30_000,
        language="vi",
    )
    keyboard = delivery_keyboard(
        primary_order_id=13,
        secrets=["key-codex-test"],
        language="vi",
        guide_url="https://token.vietshare.site/codex-api",
    )

    guide_buttons = [
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.url == "https://token.vietshare.site/codex-api"
    ]
    assert len(guide_buttons) == 1
    assert "Codex" in guide_buttons[0].text
    assert "Key kích hoạt của bạn" in text
    assert "CDK" not in text
