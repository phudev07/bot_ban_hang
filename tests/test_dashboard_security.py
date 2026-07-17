from app.dashboard_security import hash_dashboard_password, verify_dashboard_password


def test_dashboard_password_round_trip() -> None:
    encoded = hash_dashboard_password("correct horse battery staple")

    assert verify_dashboard_password("correct horse battery staple", encoded) is True
    assert verify_dashboard_password("wrong password", encoded) is False


def test_dashboard_password_rejects_malformed_hashes() -> None:
    malformed = (
        "",
        "plain-text",
        "pbkdf2_sha256$bad$bad$bad",
        "pbkdf2_sha256$999999999$%%%$%%%",
        "unknown$310000$c2FsdA==$ZGlnZXN0",
    )

    assert all(not verify_dashboard_password("password", value) for value in malformed)
