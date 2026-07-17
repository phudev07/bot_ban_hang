import base64
import binascii
import hashlib
import hmac
import os
import secrets


PASSWORD_ITERATIONS = 310_000


def hash_dashboard_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt,
        PASSWORD_ITERATIONS,
    )
    return "$".join(
        (
            "pbkdf2_sha256",
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode(),
            base64.urlsafe_b64encode(digest).decode(),
        )
    )


def verify_dashboard_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_value, digest_value = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iteration_count = int(iterations)
        if not 100_000 <= iteration_count <= 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(salt_value.encode())
        expected = base64.urlsafe_b64decode(digest_value.encode())
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            salt,
            iteration_count,
        )
    except (binascii.Error, TypeError, ValueError):
        return False
    return hmac.compare_digest(actual, expected)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)
