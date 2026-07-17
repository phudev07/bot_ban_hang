import argparse
import hashlib
import os
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


OPENSSL_HEADER = b"Salted__"
ITERATIONS = 200_000
REQUIRED_FILES = {"./postgres.sql.gz", "./application.tar.gz", "./manifest.sha256"}


def password_candidates(key_file: Path) -> list[bytes]:
    raw = key_file.read_bytes()
    first_line = raw.split(b"\n", 1)[0]
    normalized = first_line.removesuffix(b"\r")

    # OpenSSL pass:file historically kept a CR from CRLF files on Linux. Keep
    # that variant so backups made before key normalization remain recoverable.
    variants = [normalized, first_line, normalized + b"\r", raw.rstrip(b"\r\n")]
    candidates: list[bytes] = []
    for candidate in variants:
        if len(candidate) >= 32 and candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        raise ValueError("Backup key is missing or too short")
    return candidates


def decrypt_with_password(
    source: Path, destination: Path, salt: bytes, password: bytes
) -> None:
    derived = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=48,
        salt=salt,
        iterations=ITERATIONS,
    ).derive(password)
    decryptor = Cipher(
        algorithms.AES(derived[:32]),
        modes.CBC(derived[32:]),
    ).decryptor()
    unpadder = padding.PKCS7(algorithms.AES.block_size).unpadder()

    with source.open("rb") as encrypted, destination.open("wb") as output:
        encrypted.seek(16)
        while chunk := encrypted.read(1024 * 1024):
            output.write(unpadder.update(decryptor.update(chunk)))
        output.write(unpadder.update(decryptor.finalize()))
        output.write(unpadder.finalize())


def decrypt_backup(source: Path, key_file: Path, destination: Path) -> None:
    with source.open("rb") as encrypted:
        if encrypted.read(8) != OPENSSL_HEADER:
            raise ValueError("Backup does not use the expected OpenSSL salted format")
        salt = encrypted.read(8)

    partial = destination.with_suffix(destination.suffix + ".partial")
    for password in password_candidates(key_file):
        try:
            decrypt_with_password(source, partial, salt, password)
            with tarfile.open(partial, "r:gz") as archive:
                names = set(archive.getnames())
            if not REQUIRED_FILES.issubset(names):
                raise ValueError("Decrypted archive is missing required backup files")
        except (ValueError, tarfile.TarError):
            partial.unlink(missing_ok=True)
            continue
        os.replace(partial, destination)
        return

    partial.unlink(missing_ok=True)
    raise ValueError("Backup could not be decrypted with the supplied key")


def main() -> None:
    parser = argparse.ArgumentParser(description="Decrypt a VietShare shop VPS backup")
    parser.add_argument("backup", type=Path)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    decrypt_backup(args.backup.resolve(), args.key_file.resolve(), args.output.resolve())
    with args.output.open("rb") as decrypted:
        digest = hashlib.file_digest(decrypted, "sha256").hexdigest()
    print(f"Decrypted backup: {args.output.resolve()}")
    print(f"Decrypted SHA256: {digest}")


if __name__ == "__main__":
    main()
