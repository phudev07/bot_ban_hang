import hashlib
import hmac
import re
import time
import unicodedata
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

from cryptography.fernet import Fernet


def format_vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + "đ"


def format_usd_from_vnd(
    amount: int,
    vnd_per_usd: int,
    show_positive_sign: bool = False,
) -> str:
    rate = max(1, int(vnd_per_usd))
    value = (Decimal(int(amount)) / Decimal(rate)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    sign = "-" if value < 0 else "+" if show_positive_sign and value > 0 else ""
    return f"{sign}${abs(value):,.2f}"


def parse_vnd(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def find_deposit_code(text: str, prefix: str = "NAP") -> str | None:
    pattern = re.compile(
        rf"\b{re.escape(prefix.upper())}\d{{5,20}}[A-Z0-9]{{4}}\b",
        re.IGNORECASE,
    )
    matches = [match.group(0).upper() for match in pattern.finditer(text.upper())]
    return max(matches, key=len) if matches else None


def verify_sepay_hmac(
    raw_body: bytes,
    signature: str | None,
    timestamp: str | None,
    secret: str,
    *,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    if not signature or not timestamp or not secret:
        return False
    try:
        timestamp_value = int(timestamp)
    except ValueError:
        return False
    current_time = int(time.time()) if now is None else now
    if abs(current_time - timestamp_value) > tolerance_seconds:
        return False
    message = timestamp.encode("ascii") + b"." + raw_body
    expected = "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def build_sepay_qr_url(bank_code: str, account: str, amount: int, content: str) -> str:
    query = urlencode(
        {
            "acc": account,
            "bank": bank_code,
            "amount": amount,
            "des": content,
        }
    )
    return f"https://qr.sepay.vn/img?{query}"


def safe_html(value: object) -> str:
    return escape(str(value), quote=True)


# Supplier identity belongs in admin reconciliation only. Product names and
# descriptions are editable data, so redact provider names/URLs at every public
# delivery boundary instead of relying on each caller to remember the policy.
_SUPPLIER_URL_RE = re.compile(
    r"https?://[^\s<>\"']*(?:sumistore|lehaipremium|canboso|api\.dichvuright|api\.haji|rentsim|sentsim|autosms)[^\s<>\"']*",
    re.IGNORECASE,
)
_SUPPLIER_DOMAIN_RE = re.compile(
    r"\b(?:api\.)?(?:sumistore\.me|lehaipremium\.me|canboso\.com|haji\.in\.net|rentsim\.net|sentsim\.[a-z]{2,}|autosms\.site)\b|\bapi\.(?:dichvuright\.ai|haji\.in\.net)\b",
    re.IGNORECASE,
)
_SUPPLIER_NAME_RE = re.compile(
    r"(?<![\w])(?:sumistore|sumi|canboso|nce|haji|l[eê]\s*h(?:ải|ai)(?:\s*premium)?|lehai(?:premium)?|"
    r"rent\s*sim|rentsim|sentsim|auto\s*sms|autosms)(?![\w])",
    re.IGNORECASE,
)
_SUPPLIER_ERROR_RE = re.compile(
    r"\b(?:provider|supplier)_[a-z0-9_]{3,}\b",
    re.IGNORECASE,
)
_SUPPLIER_PRODUCT_ID_RE = re.compile(r"\bSP-[A-Z0-9][A-Z0-9_-]{5,}\b", re.IGNORECASE)
_SUPPLIER_ALIAS_RE = re.compile(
    r"\b(?:cdk_[a-z0-9_]+|sale_[a-z0-9_]+|gpt_bh[a-z0-9_]+)\b",
    re.IGNORECASE,
)


def sanitize_customer_text(value: object) -> str:
    """Remove supplier identities and technical source markers from public text."""
    text = str(value or "")
    text = _SUPPLIER_URL_RE.sub("nguồn hàng", text)
    text = _SUPPLIER_DOMAIN_RE.sub("nguồn hàng", text)
    text = _SUPPLIER_ERROR_RE.sub("lỗi hệ thống", text)
    text = _SUPPLIER_PRODUCT_ID_RE.sub("mã sản phẩm", text)
    text = _SUPPLIER_ALIAS_RE.sub("mã sản phẩm", text)
    return _SUPPLIER_NAME_RE.sub("nguồn hàng", text)


def safe_customer_html(value: object) -> str:
    """Sanitize public text and escape it for Telegram HTML output."""
    return safe_html(sanitize_customer_text(value))


_TELEGRAM_TAG_ALIASES = {
    "b": "b",
    "strong": "b",
    "i": "i",
    "em": "i",
    "u": "u",
    "ins": "u",
    "s": "s",
    "strike": "s",
    "del": "s",
    "code": "code",
    "pre": "pre",
    "blockquote": "blockquote",
    "a": "a",
    "tg-spoiler": "tg-spoiler",
    "tg-emoji": "tg-emoji",
}
_TELEGRAM_LINK_SCHEMES = frozenset({"http", "https", "tg", "mailto"})


def _safe_telegram_href(value: str | None) -> str | None:
    href = sanitize_customer_text(value or "").strip()
    if not href or len(href) > 2048 or any(ord(character) < 32 for character in href):
        return None
    parsed = urlsplit(href)
    if parsed.scheme.casefold() not in _TELEGRAM_LINK_SCHEMES:
        return None
    if parsed.scheme.casefold() in {"http", "https"} and not parsed.netloc:
        return None
    if parsed.scheme.casefold() == "mailto" and not parsed.path:
        return None
    if parsed.scheme.casefold() == "tg" and not (parsed.netloc or parsed.path):
        return None
    return href


class _TelegramDescriptionSanitizer(HTMLParser):
    """Build balanced Telegram HTML from the small supported tag allowlist."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[tuple[str, str | None]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.casefold()
        if normalized == "br":
            self.parts.append("\n")
            return
        output_tag = _TELEGRAM_TAG_ALIASES.get(normalized)
        if output_tag is None:
            return
        if any(opened_tag == "tg-emoji" for _, opened_tag in self.open_tags):
            return
        if output_tag == "a":
            href = _safe_telegram_href(dict(attrs).get("href"))
            self.open_tags.append((output_tag, output_tag if href else None))
            if href:
                self.parts.append(f'<a href="{escape(href, quote=True)}">')
            return
        if output_tag == "tg-emoji":
            emoji_id = dict(attrs).get("emoji-id") or ""
            valid_emoji_id = bool(re.fullmatch(r"\d{5,30}", emoji_id))
            self.open_tags.append(
                (output_tag, output_tag if valid_emoji_id else None)
            )
            if valid_emoji_id:
                self.parts.append(f'<tg-emoji emoji-id="{emoji_id}">')
            return
        self.open_tags.append((output_tag, output_tag))
        self.parts.append(f"<{output_tag}>")

    def handle_endtag(self, tag: str) -> None:
        output_tag = _TELEGRAM_TAG_ALIASES.get(tag.casefold())
        if output_tag is None:
            return
        if output_tag != "tg-emoji" and any(
            opened_tag == "tg-emoji" for _, opened_tag in self.open_tags
        ):
            return
        matching_index = next(
            (
                index
                for index in range(len(self.open_tags) - 1, -1, -1)
                if self.open_tags[index][0] == output_tag
            ),
            None,
        )
        if matching_index is None:
            return
        for _, opened_output_tag in reversed(self.open_tags[matching_index:]):
            if opened_output_tag:
                self.parts.append(f"</{opened_output_tag}>")
        del self.open_tags[matching_index:]

    def handle_data(self, data: str) -> None:
        self.parts.append(escape(sanitize_customer_text(data), quote=False))

    def sanitized_html(self) -> str:
        for _, output_tag in reversed(self.open_tags):
            if output_tag:
                self.parts.append(f"</{output_tag}>")
        self.open_tags.clear()
        return "".join(self.parts)


def safe_customer_telegram_html(value: object) -> str:
    """Keep safe Telegram formatting in editable customer-facing descriptions."""
    sanitizer = _TelegramDescriptionSanitizer()
    try:
        sanitizer.feed(sanitize_customer_text(value))
        sanitizer.close()
    except (ValueError, TypeError):
        return safe_customer_html(value)
    return sanitizer.sanitized_html()


def contains_supplier_identity(value: object) -> bool:
    """Return whether public text contains a supplier name or source marker."""
    original = str(value or "")
    return bool(original) and sanitize_customer_text(original) != original


def inventory_account_identity(raw_item: str) -> str:
    """Extract the account/key identifier without retaining its password or recovery data."""
    first_line = next((line.strip() for line in raw_item.splitlines() if line.strip()), "")
    if not first_line:
        return ""
    labelled = re.match(
        r"^(?:e-?mail|mail|user(?:name)?|account|tài\s*khoản)\s*[:=]\s*(.+)$",
        first_line,
        flags=re.IGNORECASE,
    )
    candidate = labelled.group(1).strip() if labelled else first_line
    for delimiter in ("|", "\t", ",", ";"):
        if delimiter in candidate:
            candidate = candidate.split(delimiter, 1)[0].strip()
            break
    else:
        if ":" in candidate and not re.match(r"^[a-z][a-z0-9+.-]*://", candidate, re.I):
            candidate = candidate.split(":", 1)[0].strip()
    candidate = candidate.strip().strip("\"'")
    folded_candidate = "".join(
        character
        for character in unicodedata.normalize("NFKD", candidate).casefold()
        if not unicodedata.combining(character)
    )
    if re.match(r"^(?:lien\s*he|contact)\b", folded_candidate):
        return ""
    return candidate


def normalize_inventory_identity(raw_item: str) -> str:
    identity = inventory_account_identity(raw_item)
    return unicodedata.normalize("NFKC", identity).strip().casefold()


class SecretCipher:
    def __init__(self, key: str) -> None:
        self.fernet = Fernet(key.encode())
        self.inventory_fingerprint_key = hashlib.sha256(
            b"inventory-account-fingerprint\0" + key.encode()
        ).digest()

    def encrypt(self, plaintext: str) -> str:
        return self.fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self.fernet.decrypt(ciphertext.encode()).decode()

    def inventory_fingerprint(self, raw_item: str) -> str | None:
        normalized = normalize_inventory_identity(raw_item)
        if not normalized:
            return None
        return hmac.new(
            self.inventory_fingerprint_key,
            normalized.encode(),
            hashlib.sha256,
        ).hexdigest()
