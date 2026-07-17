import hashlib
import hmac
import re
import time
from html import escape
from urllib.parse import urlencode

from cryptography.fernet import Fernet


def format_vnd(amount: int) -> str:
    return f"{amount:,}".replace(",", ".") + "đ"


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


class SecretCipher:
    def __init__(self, key: str) -> None:
        self.fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self.fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self.fernet.decrypt(ciphertext.encode()).decode()
