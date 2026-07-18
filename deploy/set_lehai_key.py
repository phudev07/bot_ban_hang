import os
import sys
import tempfile
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: set_lehai_key.py /path/to/.env")
    env_path = Path(sys.argv[1])
    key = sys.stdin.read().strip()
    if len(key) < 12 or any(character.isspace() for character in key):
        raise SystemExit("Invalid Le Hai Premium buyer key")

    replacements = {
        "LEHAI_ENABLED": "true",
        "LEHAI_API_KEY": key,
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
    print("LEHAI_CONFIG_UPDATED")


if __name__ == "__main__":
    main()
