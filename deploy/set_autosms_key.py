import base64
import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: set_autosms_key.py /path/to/.env /path/to/key.b64")
    env_path = Path(sys.argv[1])
    encoded_path = Path(sys.argv[2])
    try:
        key = base64.b64decode(encoded_path.read_bytes(), validate=True).decode().strip()
    except (ValueError, UnicodeDecodeError) as exc:
        raise SystemExit("Invalid encoded AutoSMS key") from exc
    if len(key) < 12 or any(character.isspace() for character in key):
        raise SystemExit("Invalid AutoSMS key")

    replacements = {
        "AUTOSMS_ENABLED": "true",
        "AUTOSMS_BASE_URL": "https://autosms.site",
        "AUTOSMS_API_KEY": key,
        "AUTOSMS_COUNTRY_ID": "us",
        "AUTOSMS_SERVICE_ID": "chatgpt",
        "AUTOSMS_MARKUP": "1000",
        "AUTOSMS_FALLBACK_PRICE": "1000",
        "AUTOSMS_TIMEOUT_SECONDS": "15",
        "AUTOSMS_POLL_SECONDS": "5",
        "AUTOSMS_COOLDOWN_SECONDS": "60",
        "AUTOSMS_SNAPSHOT_CACHE_SECONDS": "10",
        "AUTOSMS_REQUEST_RECOVERY_SECONDS": "120",
        "AUTOSMS_PENDING_ALERT_SECONDS": "900",
    }
    lines = env_path.read_text(encoding="utf-8").splitlines()
    updated: set[str] = set()
    output: list[str] = []
    for line in lines:
        name = line.split("=", 1)[0].strip()
        if name in replacements:
            output.append(f"{name}={replacements[name]}")
            updated.add(name)
        else:
            output.append(line)
    for name, value in replacements.items():
        if name not in updated:
            output.append(f"{name}={value}")

    mode = env_path.stat().st_mode
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=env_path.parent,
        delete=False,
    ) as target:
        target.write("\n".join(output) + "\n")
        temporary_path = Path(target.name)
    os.chmod(temporary_path, mode)
    temporary_path.replace(env_path)
    encoded_path.unlink(missing_ok=True)
    print("AUTOSMS_CONFIG_UPDATED")


if __name__ == "__main__":
    main()
