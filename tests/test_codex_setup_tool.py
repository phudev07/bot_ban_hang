from pathlib import Path
from zipfile import ZipFile


def test_codex_setup_tool_is_portable_and_restorable() -> None:
    source = Path("deploy/CodexVietShareSetup.cs").read_text(encoding="utf-8")
    binary_path = Path("app/static/VietShare-Codex-Claude-Setup.exe")
    archive_path = Path("app/static/VietShare-Codex-Claude-Setup.zip")
    binary = binary_path.read_bytes()

    assert binary.startswith(b"MZ")
    assert len(binary) < 1_000_000
    assert archive_path.read_bytes().startswith(b"PK")
    with ZipFile(archive_path) as archive:
        assert archive.namelist() == ["VietShare-Codex-Claude-Setup.exe"]
        assert archive.read("VietShare-Codex-Claude-Setup.exe").startswith(b"MZ")
    assert '"cx/gpt-5.6-sol"' in source
    assert '"cx/gpt-5.5"' in source
    assert '"claude-opus-5"' in source
    assert '"claude-sonnet-5"' in source
    assert '"claude-haiku-4-5"' in source
    assert '"claude-fable-5"' in source
    assert '"https://gateway.dichvuright.ai/v1"' in source
    assert '"https://gateway.dichvuright.ai"' in source
    assert "requires_openai_auth = true\\r\\n" in source
    assert "default_subagent_model = \\\"" in source
    assert "ANTHROPIC_AUTH_TOKEN" in source
    assert "ANTHROPIC_DEFAULT_FABLE_MODEL" in source
    assert "EnsureCodexBackupState" in source
    assert "EnsureClaudeBackupState" in source
    assert "RestoreFile" in source
    assert "File.Replace" in source
    assert "ValidKey" in source
