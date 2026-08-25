import importlib.util
import json
import sqlite3
from pathlib import Path
from urllib.error import HTTPError, URLError


TOOL_PATH = Path(__file__).parents[1] / "downloads" / "import_to_9router_macos.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("macos_import_tool", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.payload


def account(index: int, *, email: str | None = None) -> dict[str, object]:
    return {
        "email": email or f"user{index}@example.com",
        "accessToken": f"access-{index}",
        "refreshToken": f"refresh-{index}",
        "expiresIn": 864000,
    }


def create_router_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE providerConnections (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                authType TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                priority INTEGER NOT NULL,
                isActive INTEGER NOT NULL,
                data TEXT NOT NULL,
                createdAt TEXT NOT NULL,
                updatedAt TEXT NOT NULL
            )
            """
        )


def test_macos_database_discovery_handles_library_path_and_spaces(monkeypatch, tmp_path) -> None:
    tool = load_tool()
    monkeypatch.setattr(tool.sys, "platform", "darwin")
    home = tmp_path / "Mac User With Spaces"
    database = home / "Library" / "Application Support" / "9Router" / "db" / "data.sqlite"
    database.parent.mkdir(parents=True)
    database.touch()
    monkeypatch.setattr(tool.os.path, "expanduser", lambda _path: str(home))

    existing, checked = tool.find_9router_db_paths()

    assert str(database) in existing
    assert str(database) in checked
    assert all("AppData/Roaming" not in path.replace("\\", "/") for path in checked)
    assert all("/var/lib/" not in path.replace("\\", "/") for path in checked)


def test_macos_parser_supports_json_array_jsonl_and_combined_rows(tmp_path) -> None:
    tool = load_tool()
    path = tmp_path / "tokens with spaces.json"
    path.write_text(
        json.dumps(account(1))
        + "\n"
        + json.dumps(account(2))
        + "\n"
        + "third@example.com|password|2fa|"
        + json.dumps(account(3, email="third@example.com")),
        encoding="utf-8",
    )

    parsed, total_lines, description = tool.parse_accounts_from_file(str(path))

    assert len(parsed) == 3
    assert total_lines == 3
    assert "JSONL" in description
    assert tool.deduplicate_accounts(parsed) == parsed

    array_path = tmp_path / "array.json"
    array_path.write_text(json.dumps([account(4), account(5)]), encoding="utf-8")
    array, _, array_description = tool.parse_accounts_from_file(str(array_path))
    assert len(array) == 2
    assert array_description == "JSON Array"


def test_macos_parser_rejects_missing_empty_and_invalid_files(tmp_path) -> None:
    tool = load_tool()
    missing = tool.parse_accounts_from_file(str(tmp_path / "missing.json"))
    assert missing[0] == []

    empty = tmp_path / "empty.json"
    empty.write_text("   ", encoding="utf-8")
    assert tool.parse_accounts_from_file(str(empty))[0] == []

    invalid = tmp_path / "invalid.txt"
    invalid.write_text("email|password|no-access-token", encoding="utf-8")
    parsed, _, description = tool.parse_accounts_from_file(str(invalid))
    assert parsed == []
    assert "Khong tim thay" in description


def test_macos_api_import_sends_json_and_reports_partial_success(monkeypatch) -> None:
    tool = load_tool()
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["content_type"] = request.headers["Content-type"]
        return FakeResponse(201, b'{"success": 1, "failed": 1}')

    monkeypatch.setattr(tool.urllib.request, "urlopen", fake_urlopen)
    ok, message = tool.import_via_api([account(1), account(2)], "http://127.0.0.1:20128/import")

    assert ok is True
    assert message == "Thanh cong qua API"
    assert captured == {
        "url": "http://127.0.0.1:20128/import",
        "timeout": 10,
        "body": [account(1), account(2)],
        "content_type": "application/json",
    }


def test_macos_api_failure_can_fall_back_to_sqlite(monkeypatch, tmp_path) -> None:
    tool = load_tool()
    database = tmp_path / "router.sqlite"
    create_router_db(database)

    def unavailable(_request, timeout):
        assert timeout == 10
        raise URLError("Connection refused")

    monkeypatch.setattr(tool.urllib.request, "urlopen", unavailable)
    api_ok, api_message = tool.import_via_api([account(1)], "http://127.0.0.1:20128/import")
    assert api_ok is False
    assert "Khong the ket noi" in api_message

    sqlite_ok, sqlite_message = tool.import_via_sqlite([account(1)], str(database))
    assert sqlite_ok is True
    assert sqlite_message == "Thanh cong qua SQLite"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT provider, email, isActive, data FROM providerConnections"
        ).fetchone()
    assert row[0:3] == ("codex", "user1@example.com", 1)
    assert json.loads(row[3])["accessToken"] == "access-1"


def test_macos_sqlite_import_updates_existing_email_without_duplicate(monkeypatch, tmp_path) -> None:
    tool = load_tool()
    database = tmp_path / "router.sqlite"
    create_router_db(database)
    first = account(1)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO providerConnections
              (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
            VALUES ('existing', 'codex', 'oauth', 'Old name', ?, 1, 0, '{}', 'old', 'old')
            """,
            (first["email"],),
        )

    ok, message = tool.import_via_sqlite([account(9, email=first["email"])], str(database))

    assert ok is True
    assert message == "Thanh cong qua SQLite"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT id, isActive, data FROM providerConnections"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "existing"
    assert rows[0][1] == 1
    assert json.loads(rows[0][2])["accessToken"] == "access-9"


def test_macos_api_http_error_is_safe_and_does_not_expose_full_response(monkeypatch) -> None:
    tool = load_tool()

    def rejected(_request, timeout):
        assert timeout == 10
        raise HTTPError(
            "http://127.0.0.1:20128/import",
            413,
            "Payload Too Large",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr(tool.urllib.request, "urlopen", rejected)
    ok, message = tool.import_via_api([account(1)], "http://127.0.0.1:20128/import")

    assert ok is False
    assert message == "HTTP 413: Payload Too Large"
