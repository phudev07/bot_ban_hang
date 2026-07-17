import io
import os
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from deploy.decrypt_backup import ITERATIONS, decrypt_backup


def make_encrypted_backup(path: Path, password: bytes) -> None:
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        for name in ("postgres.sql.gz", "application.tar.gz", "manifest.sha256"):
            content = f"fixture:{name}".encode()
            info = tarfile.TarInfo(f"./{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    salt = os.urandom(8)
    derived = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=48,
        salt=salt,
        iterations=ITERATIONS,
    ).derive(password)
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(archive_buffer.getvalue()) + padder.finalize()
    encryptor = Cipher(
        algorithms.AES(derived[:32]),
        modes.CBC(derived[32:]),
    ).encryptor()
    path.write_bytes(b"Salted__" + salt + encryptor.update(padded) + encryptor.finalize())


@pytest.mark.parametrize(
    ("key_file_bytes", "encryption_password"),
    [
        (b"k" * 64 + b"\r\n", b"k" * 64),
        (b"k" * 64 + b"\r\n", b"k" * 64 + b"\r"),
        (b"k" * 64, b"k" * 64 + b"\r"),
    ],
)
def test_decrypt_backup_supports_normalized_and_legacy_keys(
    tmp_path: Path, key_file_bytes: bytes, encryption_password: bytes
) -> None:
    source = tmp_path / "backup.tar.gz.enc"
    key_file = tmp_path / "backup.key"
    destination = tmp_path / "backup.tar.gz"
    key_file.write_bytes(key_file_bytes)
    make_encrypted_backup(source, encryption_password)

    decrypt_backup(source, key_file, destination)

    with tarfile.open(destination, "r:gz") as archive:
        assert "./postgres.sql.gz" in archive.getnames()


def test_decrypt_backup_removes_partial_file_after_wrong_key(tmp_path: Path) -> None:
    source = tmp_path / "backup.tar.gz.enc"
    key_file = tmp_path / "backup.key"
    destination = tmp_path / "backup.tar.gz"
    key_file.write_bytes(b"x" * 64)
    make_encrypted_backup(source, b"y" * 64)

    with pytest.raises(ValueError, match="could not be decrypted"):
        decrypt_backup(source, key_file, destination)

    assert not destination.exists()
    assert not destination.with_suffix(".gz.partial").exists()
