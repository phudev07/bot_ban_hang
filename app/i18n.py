TEXT = {
    "vi": {
        "products": "🛍 Mặt hàng",
        "quick": "⚡ Mua nhanh",
        "deposit": "💳 Nạp tiền",
        "codes": "🔑 Lấy code",
        "sms": "📲 Thuê số SMS",
        "orders": "📦 Đơn mua",
        "profile": "👤 Hồ sơ",
        "warehouse_api": "🔌 API đấu kho",
        "referral": "🎁 Giới thiệu bạn bè",
        "support": "🆘 Hỗ trợ",
        "clear": "🧹 Dọn chat",
        "language": "🇻🇳 VN / 🇺🇸 US",
        "back": "⬅️ Quay lại",
        "buy": "✅ Mua ngay",
        "other_amount": "✍️ Số tiền khác",
    },
    "en": {
        "products": "🛍 Products",
        "quick": "⚡ Quick buy",
        "deposit": "💳 Deposit",
        "codes": "🔑 My codes",
        "sms": "📲 Rent SMS number",
        "orders": "📦 Orders",
        "profile": "👤 Profile",
        "warehouse_api": "🔌 Warehouse API",
        "referral": "🎁 Refer friends",
        "support": "🆘 Support",
        "clear": "🧹 Clear menu",
        "language": "🇻🇳 VN / 🇺🇸 US",
        "back": "⬅️ Back",
        "buy": "✅ Buy now",
        "other_amount": "✍️ Other amount",
    },
}


def tr(language: str, key: str) -> str:
    return TEXT.get(language, TEXT["vi"]).get(key, key)
