import gzip
import hashlib
import io
import os
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from deploy.decrypt_backup import ITERATIONS, decrypt_backup
from deploy.verify_backup import verify_encrypted_backup


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


def make_verifiable_encrypted_backup(
    path: Path,
    password: bytes,
    *,
    corrupt_manifest: bool = False,
) -> None:
    application_buffer = io.BytesIO()
    with tarfile.open(fileobj=application_buffer, mode="w:gz") as archive:
        root = tarfile.TarInfo(".")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, content in (
            ("./.env", b"SECRET=test\n"),
            ("./app/main.py", b"print('app')\n"),
            ("./docker-compose.yml", b"services: {}\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    system_config_buffer = io.BytesIO()
    with tarfile.open(fileobj=system_config_buffer, mode="w:gz") as archive:
        for name in (
            "etc/apt/apt.conf.d/20auto-upgrades",
            "etc/apt/sources.list",
            "etc/caddy/Caddyfile",
            "etc/ssh/sshd_config.d/99-hardening.conf",
            "etc/sysctl.d/99-telegram-shop.conf",
            "etc/ufw/ufw.conf",
            "etc/ufw/user.rules",
        ):
            content = f"fixture:{name}\n".encode()
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    payloads = {
        "postgres.sql.gz": gzip.compress(
            b"CREATE TABLE public.users (id bigint);\n"
            b"COPY public.users (id) FROM stdin;\n1\n\\.\n"
        ),
        "redis.rdb": b"REDIS0011fixture",
        "application.tar.gz": application_buffer.getvalue(),
        "system-config.tar.gz": system_config_buffer.getvalue(),
        "metadata.txt": b"created_at_utc=fixture\n",
        "RESTORE.txt": b"restore fixture\n",
    }
    manifest_lines = []
    for name, content in payloads.items():
        digest = hashlib.sha256(content).hexdigest()
        if corrupt_manifest and name == "redis.rdb":
            digest = "0" * 64
        manifest_lines.append(f"{digest}  {name}\n")
    payloads["manifest.sha256"] = "".join(manifest_lines).encode()

    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        for name, content in payloads.items():
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


def test_verify_encrypted_backup_checks_database_application_and_manifest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "backup.tar.gz.enc"
    key_file = tmp_path / "backup.key"
    key_file.write_bytes(b"k" * 64)
    make_verifiable_encrypted_backup(source, b"k" * 64)

    result = verify_encrypted_backup(source, key_file)

    assert result.manifest_entries == 6
    assert result.postgres_uncompressed_bytes > 0
    assert result.application_entries >= 3
    assert result.system_config_entries >= 7


def test_verify_encrypted_backup_rejects_manifest_mismatch(tmp_path: Path) -> None:
    source = tmp_path / "backup.tar.gz.enc"
    key_file = tmp_path / "backup.key"
    key_file.write_bytes(b"k" * 64)
    make_verifiable_encrypted_backup(source, b"k" * 64, corrupt_manifest=True)

    with pytest.raises(ValueError, match="checksum mismatch: redis.rdb"):
        verify_encrypted_backup(source, key_file)
