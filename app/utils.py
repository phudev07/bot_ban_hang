import hashlib
import hmac
import re
import time
import unicodedata
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
