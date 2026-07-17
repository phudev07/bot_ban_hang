from app.delivery import (
    codex_auth_content,
    codex_config_content,
    codex_setup_keyboard,
    codex_setup_text,
    delivery_file,
    delivery_keyboard,
    delivery_text,
    router_token_delivery_text,
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
    assert "#11, #12" not in text
    assert keyboard.inline_keyboard[0][0].copy_text.text == secrets[0]
    assert keyboard.inline_keyboard[1][0].copy_text.text == secrets[1]
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
    assert "1. first:secret" in content
    assert "2. second:secret" in content


def test_codex_setup_uses_copyable_code_blocks() -> None:
    config = codex_config_content("https://token.vietshare.site/v1/")
    auth = codex_auth_content("sk-test-customer-key")
    config_text = codex_setup_text(
        filename="~/.codex/config.toml",
        content=config,
        code_language="toml",
        step=1,
        language="vi",
    )
    auth_text = codex_setup_text(
        filename="~/.codex/auth.json",
        content=auth,
        code_language="json",
        step=2,
        language="vi",
    )
    config_keyboard = codex_setup_keyboard(
        filename="config.toml",
        content=config,
        language="vi",
    )

    assert 'base_url = "https://token.vietshare.site/v1"' in config
    assert config.count('model = "cx/gpt-5.6-sol"') == 2
    assert '<pre><code class="language-toml">' in config_text
    assert '<pre><code class="language-json">' in auth_text
    assert "%USERPROFILE%\\.codex" in config_text
    assert "C:\\Users\\tên-user\\.codex" in config_text
    assert "Mở lại thư mục <code>.codex</code> ở bước 1." in auth_text
    assert '"OPENAI_API_KEY": "sk-test-customer-key"' in auth
    assert config_keyboard.inline_keyboard[0][0].copy_text.text == config
    assert len(config) <= 256


def test_router_token_delivery_omits_shared_quota_explanation() -> None:
    text = router_token_delivery_text(
        shop_order_code="RT-TEST",
        product_name="GPT Token 9Router",
        api_url="https://token.vietshare.site/v1",
        api_key="sk-test-key",
        token_quota=10_000_000,
        paid_amount=10_000,
        language="vi",
    )

    assert "Input và output dùng chung quota" not in text
    assert "Key tự ngắt khi số token còn lại về 0." in text
