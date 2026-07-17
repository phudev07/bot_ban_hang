TEXT = {
    "vi": {
        "products": "🛍 Mặt hàng",
        "quick": "⚡ Mua nhanh",
        "deposit": "💳 Nạp tiền",
        "codes": "🔑 Lấy code",
        "orders": "📦 Đơn mua",
        "profile": "👤 Hồ sơ",
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
        "orders": "📦 Orders",
        "profile": "👤 Profile",
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
