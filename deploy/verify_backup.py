import argparse
import gzip
import hashlib
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

if __package__:
    from deploy.decrypt_backup import decrypt_backup
else:
    from decrypt_backup import decrypt_backup


REQUIRED_MANIFEST_PAYLOADS = {
    "postgres.sql.gz",
    "redis.rdb",
    "application.tar.gz",
    "system-config.tar.gz",
    "metadata.txt",
    "RESTORE.txt",
}
REQUIRED_APPLICATION_FILES = {".env", "app/main.py", "docker-compose.yml"}
REQUIRED_SYSTEM_CONFIG_FILES = {
    "etc/apt/apt.conf.d/20auto-upgrades",
    "etc/apt/sources.list",
    "etc/caddy/Caddyfile",
    "etc/ssh/sshd_config.d/99-hardening.conf",
    "etc/sysctl.d/99-telegram-shop.conf",
    "etc/ufw/ufw.conf",
    "etc/ufw/user.rules",
}
SQL_MARKERS = (b"CREATE TABLE public.users", b"COPY public.users")


@dataclass(frozen=True)
class BackupVerification:
    manifest_entries: int
    postgres_uncompressed_bytes: int
    application_entries: int
    system_config_entries: int


def _normalize_archive_name(name: str) -> str:
    normalized = str(PurePosixPath(name.removeprefix("./")))
    if normalized in {"", "."} or normalized.startswith("../") or normalized.startswith("/"):
        raise ValueError(f"Unsafe backup archive path: {name}")
    return normalized


def _archive_members(archive: tarfile.TarFile) -> dict[str, tarfile.TarInfo]:
    members: dict[str, tarfile.TarInfo] = {}
    for member in archive.getmembers():
        if not member.isfile():
            continue
        normalized = _normalize_archive_name(member.name)
        if normalized in members:
            raise ValueError(f"Duplicate backup archive entry: {normalized}")
        members[normalized] = member
    return members


def _read_manifest(archive: tarfile.TarFile, member: tarfile.TarInfo) -> dict[str, str]:
    source = archive.extractfile(member)
    if source is None:
        raise ValueError("Backup manifest could not be read")
    entries: dict[str, str] = {}
    for raw_line in source.read().decode("utf-8", errors="strict").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError("Backup manifest contains an invalid checksum line")
        try:
            int(parts[0], 16)
        except ValueError as exc:
            raise ValueError("Backup manifest contains an invalid SHA256 value") from exc
        name = _normalize_archive_name(parts[1].lstrip("*"))
        if name in entries:
            raise ValueError(f"Backup manifest contains duplicate entry: {name}")
        entries[name] = parts[0].lower()
    return entries


def _copy_and_hash_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    destination: Path,
) -> str:
    source = archive.extractfile(member)
    if source is None:
        raise ValueError(f"Backup payload could not be read: {member.name}")
    digest = hashlib.sha256()
    with destination.open("wb") as target:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            target.write(chunk)
    return digest.hexdigest()


def _verify_postgres_dump(path: Path) -> int:
    found = {marker: False for marker in SQL_MARKERS}
    total_bytes = 0
    tail = b""
    try:
        with gzip.open(path, "rb") as source:
            while chunk := source.read(1024 * 1024):
                total_bytes += len(chunk)
                searchable = tail + chunk
                for marker in SQL_MARKERS:
                    found[marker] = found[marker] or marker in searchable
                tail = searchable[-128:]
    except (EOFError, OSError) as exc:
        raise ValueError("PostgreSQL dump is not a complete gzip file") from exc
    missing = [marker.decode() for marker, present in found.items() if not present]
    if missing:
        raise ValueError(f"PostgreSQL dump is missing required SQL markers: {', '.join(missing)}")
    return total_bytes


def _verify_application_archive(path: Path) -> int:
    try:
        with tarfile.open(path, "r:gz") as archive:
            names = {
                _normalize_archive_name(member.name)
                for member in archive.getmembers()
                if member.isfile()
            }
    except tarfile.TarError as exc:
        raise ValueError("Application payload is not a complete tar.gz archive") from exc
    missing = REQUIRED_APPLICATION_FILES - names
    if missing:
        raise ValueError(
            "Application payload is missing required files: " + ", ".join(sorted(missing))
        )
    return len(names)


def _verify_system_config_archive(path: Path) -> int:
    try:
        with tarfile.open(path, "r:gz") as archive:
            names = {
                _normalize_archive_name(member.name)
                for member in archive.getmembers()
                if member.isfile()
            }
    except tarfile.TarError as exc:
        raise ValueError("System configuration payload is not a complete tar.gz archive") from exc
    missing = REQUIRED_SYSTEM_CONFIG_FILES - names
    if missing:
        raise ValueError(
            "System configuration payload is missing required files: "
            + ", ".join(sorted(missing))
        )
    return len(names)


def verify_decrypted_backup(path: Path) -> BackupVerification:
    with tempfile.TemporaryDirectory(prefix="shop-backup-verify-") as temp_directory:
        temp_root = Path(temp_directory)
        try:
            with tarfile.open(path, "r:gz") as archive:
                members = _archive_members(archive)
                manifest_member = members.get("manifest.sha256")
                if manifest_member is None:
                    raise ValueError("Backup archive is missing manifest.sha256")
                manifest = _read_manifest(archive, manifest_member)
                missing_payloads = REQUIRED_MANIFEST_PAYLOADS - set(manifest)
                if missing_payloads:
                    raise ValueError(
                        "Backup manifest is missing payloads: "
                        + ", ".join(sorted(missing_payloads))
                    )

                payload_paths: dict[str, Path] = {}
                for name, expected_digest in manifest.items():
                    member = members.get(name)
                    if member is None:
                        raise ValueError(f"Backup payload listed in manifest is missing: {name}")
                    destination = temp_root / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    actual_digest = _copy_and_hash_member(archive, member, destination)
                    if actual_digest != expected_digest:
                        raise ValueError(f"Backup payload checksum mismatch: {name}")
                    payload_paths[name] = destination
        except tarfile.TarError as exc:
            raise ValueError("Decrypted backup is not a complete tar.gz archive") from exc

        postgres_bytes = _verify_postgres_dump(payload_paths["postgres.sql.gz"])
        application_entries = _verify_application_archive(payload_paths["application.tar.gz"])
        system_config_entries = _verify_system_config_archive(
            payload_paths["system-config.tar.gz"]
        )
        return BackupVerification(
            manifest_entries=len(manifest),
            postgres_uncompressed_bytes=postgres_bytes,
            application_entries=application_entries,
            system_config_entries=system_config_entries,
        )


def verify_encrypted_backup(source: Path, key_file: Path) -> BackupVerification:
    with tempfile.TemporaryDirectory(prefix="shop-backup-decrypt-") as temp_directory:
        decrypted = Path(temp_directory) / "backup.tar.gz"
        decrypt_backup(source, key_file, decrypted)
        return verify_decrypted_backup(decrypted)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decrypt and fully verify a VietShare shop VPS backup"
    )
    parser.add_argument("backup", type=Path)
    parser.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()

    result = verify_encrypted_backup(args.backup.resolve(), args.key_file.resolve())
    print(f"Backup verified: {args.backup.resolve()}")
    print(f"Manifest payloads: {result.manifest_entries}")
    print(f"PostgreSQL bytes checked: {result.postgres_uncompressed_bytes}")
    print(f"Application entries checked: {result.application_entries}")
    print(f"System configuration entries checked: {result.system_config_entries}")


if __name__ == "__main__":
    main()
