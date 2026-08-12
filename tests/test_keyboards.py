from app.keyboards import (
    codex_products_menu,
    main_menu,
    order_history_menu,
    product_detail,
    products_menu,
    purchase_payment_options,
    quick_access_keyboard,
    quantity_menu,
    seller_purchase_confirmation_menu,
    sms_rental_menu,
    SmsRentalSourceButton,
    warehouse_api_menu,
)
from app.models import Order, Product


def make_product() -> Product:
    return Product(
        id=10,
        category_id=2,
        name_vi="Tài khoản",
        name_en="Account",
        price=20_000,
        allow_quantity=True,
        max_quantity=10,
    )


def test_out_of_stock_product_has_no_buy_button() -> None:
    keyboard = product_detail(make_product(), "vi", stock=0)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert callbacks == ["cat:2"]


def test_out_of_stock_product_button_is_red_and_clearly_labelled() -> None:
    available = make_product()
    available.external_stock = 3
    sold_out = Product(
        id=11,
        category_id=2,
        name_vi="Hết hàng",
        name_en="Sold out",
        price=30_000,
        external_stock=0,
    )

    keyboard = products_menu([available, sold_out], "vi", "back:menu")
    available_button = keyboard.inline_keyboard[0][0]
    sold_out_button = keyboard.inline_keyboard[1][0]

    assert available_button.style == "success"
    assert sold_out_button.style == "danger"
    assert sold_out_button.text == "🔴 Hết hàng · 30.000đ · Hết hàng"


def test_local_inventory_menu_stock_overrides_stale_external_stock() -> None:
    product = make_product()
    product.external_stock = 0
    product._menu_stock = 15

    keyboard = products_menu([product], "vi", "back:menu")
    button = keyboard.inline_keyboard[0][0]

    assert button.style == "success"
    assert "Hết hàng" not in button.text


def test_quick_buy_product_returns_to_quick_buy_list() -> None:
    product = make_product()
    product.external_stock = 3
    listing = products_menu(
        [product],
        "vi",
        "back:menu",
        origin="quick",
    )
    assert listing.inline_keyboard[0][0].callback_data == "prod:10:quick"

    detail = product_detail(product, "vi", stock=3, origin="quick")
    callbacks = [button.callback_data for row in detail.inline_keyboard for button in row]
    assert "qtymenu:10:origin:quick" in callbacks
    assert "menu:quick" in callbacks

    quantities = quantity_menu(product, "vi", stock=3, origin="quick")
    assert quantities.inline_keyboard[-1][0].callback_data == "prod:10:quick"


def test_main_menu_exposes_warehouse_api_and_referrals() -> None:
    keyboard = main_menu("vi", sms_enabled=True, codex_enabled=True)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "menu:warehouse-api" in callbacks
    assert "menu:referral" in callbacks
    assert "menu:sms" in callbacks
    assert "menu:codex-api" in callbacks
    assert "menu:preorders" in callbacks
    rows = [[button.callback_data for button in row] for row in keyboard.inline_keyboard]
    assert rows.index(["menu:codex-api"]) == rows.index(["menu:sms"]) + 1


def test_main_menu_hides_sms_until_provider_is_configured() -> None:
    keyboard = main_menu("vi")
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "menu:sms" not in callbacks
    assert "menu:codex-api" not in callbacks


def test_codex_product_navigation_returns_to_dedicated_menu() -> None:
    product = make_product()
    product.external_stock = 2
    listing = codex_products_menu([product], "vi")
    assert listing.inline_keyboard[0][0].callback_data == "prod:10:codex"

    detail = product_detail(product, "vi", stock=2, origin="codex")
    callbacks = [button.callback_data for row in detail.inline_keyboard for button in row]
    assert "menu:codex-api" in callbacks


def test_sms_menu_shows_one_button_per_available_country() -> None:
    keyboard = sms_rental_menu(
        "vi",
        sources=[
            SmsRentalSourceButton(
                "1", "+1 American", "+1 American", 2_000, 5, True
            ),
            SmsRentalSourceButton(
                "855", "+855 Cambodia", "+855 Cambodia", 2_000, 3, True
            ),
        ],
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    callbacks = [button.callback_data for button in buttons]

    assert callbacks[:2] == ["sms:rent:1", "sms:rent:855"]
    assert "🇺🇸 Thuê số +1 American · 2.000đ" == buttons[0].text
    assert "🇰🇭 Thuê số +855 Cambodia · 2.000đ" == buttons[1].text


def test_sms_menu_hides_each_country_without_available_stock() -> None:
    one_available = sms_rental_menu(
        "vi",
        sources=[
            SmsRentalSourceButton(
                "1", "+1 American", "+1 American", 2_000, 0, True
            ),
            SmsRentalSourceButton(
                "855", "+855 Cambodia", "+855 Cambodia", 2_000, 2, True
            ),
        ],
    )
    none_available = sms_rental_menu(
        "vi",
        sources=[
            SmsRentalSourceButton(
                "1", "+1 American", "+1 American", 2_000, 0, True
            ),
            SmsRentalSourceButton(
                "855", "+855 Cambodia", "+855 Cambodia", 2_000, 0, False
            ),
        ],
    )

    one_callbacks = [
        button.callback_data
        for row in one_available.inline_keyboard
        for button in row
    ]
    none_callbacks = [
        button.callback_data
        for row in none_available.inline_keyboard
        for button in row
    ]
    assert "sms:rent:1" not in one_callbacks
    assert "sms:rent:855" in one_callbacks
    assert "sms:rent:1" not in none_callbacks
    assert "sms:rent:855" not in none_callbacks


def test_quick_access_keyboard_is_one_persistent_row() -> None:
    keyboard = quick_access_keyboard("vi")

    assert [[button.text for button in row] for row in keyboard.keyboard] == [
        ["☰ Menu", "⚡ Mua nhanh", "💳 Nạp tiền"]
    ]
    assert keyboard.resize_keyboard is True
    assert keyboard.is_persistent is True


def test_warehouse_api_guide_opens_public_documentation() -> None:
    keyboard = warehouse_api_menu(
        "vi",
        active=True,
        docs_url="https://token.vietshare.site/docs",
    )
    guide_button = keyboard.inline_keyboard[0][0]

    assert guide_button.url == "https://token.vietshare.site/docs"
    assert guide_button.callback_data is None


def test_quantity_buttons_do_not_exceed_available_stock() -> None:
    keyboard = quantity_menu(make_product(), "vi", stock=3)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "buy:10:1" in callbacks
    assert "buy:10:2" in callbacks
    assert "buy:10:5" not in callbacks
    assert "buy:10:10" not in callbacks
    assert "customqty:10" in callbacks


def test_seller_quantity_buttons_do_not_show_a_false_unit_price() -> None:
    keyboard = quantity_menu(
        make_product(),
        "vi",
        stock=3,
        unit_price=35_000,
        variable_price=True,
    )
    texts = [button.text for row in keyboard.inline_keyboard for button in row]

    assert "1 tài khoản · xem tổng" in texts
    assert "2 tài khoản · xem tổng" in texts
    assert all("35.000" not in text for text in texts)

    confirmation = seller_purchase_confirmation_menu(10, 2, 71_000, "vi")
    assert confirmation.inline_keyboard[0][0].callback_data == "sellerbuy:10:2:71000"


def test_flash_sale_callbacks_keep_the_campaign_identity() -> None:
    detail = product_detail(make_product(), "vi", stock=3, flash_sale_id=77)
    detail_callbacks = [
        button.callback_data for row in detail.inline_keyboard for button in row
    ]
    assert "qtymenu:10:flash:77" in detail_callbacks

    quantities = quantity_menu(
        make_product(),
        "vi",
        stock=3,
        unit_price=15_000,
        flash_sale_id=77,
    )
    quantity_callbacks = [
        button.callback_data for row in quantities.inline_keyboard for button in row
    ]
    assert "buy:10:1:flash:77" in quantity_callbacks
    assert "buy:10:2:flash:77" in quantity_callbacks
    assert "customqty:10:flash:77" in quantity_callbacks

    payment = purchase_payment_options(10, 2, "vi", flash_sale_id=77)
    assert payment.inline_keyboard[0][0].callback_data == "directpay:10:2:flash:77"


def test_product_detail_offers_product_specific_discount_code() -> None:
    keyboard = product_detail(make_product(), "vi", stock=3)
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "coupon:10" in callbacks


def test_order_history_groups_items_under_one_shop_order_code() -> None:
    product = make_product()
    orders = [
        Order(
            id=order_id,
            user_id=123,
            product_id=product.id,
            inventory_item_id=order_id,
            amount=20_000,
            batch_code="BORDER123",
            product=product,
        )
        for order_id in (11, 12)
    ]

    keyboard = order_history_menu(orders, "vi")
    first_button = keyboard.inline_keyboard[0][0]

    assert first_button.callback_data == "orderdetail:11"
    assert first_button.text == "BORDER123 · Tài khoản · 2 tài khoản"


def test_order_history_keeps_the_name_saved_at_purchase_time() -> None:
    product = make_product()
    order = Order(
        id=21,
        user_id=123,
        product_id=product.id,
        product_name_vi="Tên lúc khách mua",
        product_name_en="Name at purchase",
        inventory_item_id=21,
        amount=20_000,
        product=product,
    )
    product.name_vi = "Tên mới của sản phẩm"
    product.name_en = "New product name"

    vi_button = order_history_menu([order], "vi").inline_keyboard[0][0]
    en_button = order_history_menu([order], "en").inline_keyboard[0][0]

    assert "Tên lúc khách mua" in vi_button.text
    assert "Name at purchase" in en_button.text


def test_legacy_order_name_falls_back_to_the_current_product() -> None:
    product = make_product()
    order = Order(
        id=22,
        user_id=123,
        product_id=product.id,
        inventory_item_id=22,
        amount=20_000,
        product=product,
    )

    assert order.display_name_vi == product.name_vi
    assert order.display_name_en == product.name_en
