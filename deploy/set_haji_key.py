import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: set_haji_key.py /path/to/.env")
    env_path = Path(sys.argv[1])
    key = sys.stdin.read().strip()
    if (
        not key.startswith(("dl_", "sk-"))
        or len(key) < 16
        or any(character.isspace() for character in key)
    ):
        raise SystemExit("Invalid Haji dealer API key")

    replacements = {
        "HAJI_ENABLED": "true",
        "HAJI_BASE_URL": "https://api.haji.in.net",
        "HAJI_API_KEY": key,
        "HAJI_MARKUP": "5000",
        "HAJI_TIMEOUT_SECONDS": "15",
        "HAJI_SYNC_SECONDS": "60",
        "HAJI_AUDIT_SECONDS": "30",
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
    print("HAJI_CONFIG_UPDATED")


if __name__ == "__main__":
    main()
