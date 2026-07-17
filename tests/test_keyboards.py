from app.keyboards import (
    main_menu,
    order_history_menu,
    product_detail,
    quantity_menu,
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


def test_main_menu_exposes_warehouse_api_and_referrals() -> None:
    keyboard = main_menu("vi")
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]

    assert "menu:warehouse-api" in callbacks
    assert "menu:referral" in callbacks


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
