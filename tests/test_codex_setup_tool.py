from pathlib import Path


def test_codex_setup_tool_is_portable_and_restorable() -> None:
    source = Path("deploy/CodexVietShareSetup.cs").read_text(encoding="utf-8")
    binary = Path("app/static/VietShare-Codex-Setup.exe").read_bytes()

    assert binary.startswith(b"MZ")
    assert len(binary) < 1_000_000
    assert '"cx/gpt-5.6-sol"' in source
    assert '"cx/gpt-5.5"' in source
    assert '"https://gateway.dichvuright.ai/v1"' in source
    assert "requires_openai_auth = true\\r\\n" in source
    assert "default_subagent_model = \\\"" in source
    assert "EnsureBackupState" in source
    assert "RestoreFile" in source
    assert "File.Replace" in source
    assert "ValidKey" in source
