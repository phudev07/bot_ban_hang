from app.dashboard_security import LoginRateLimiter, hash_dashboard_password, verify_dashboard_password


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


def test_login_rate_limiter_blocks_and_resets() -> None:
    limiter = LoginRateLimiter(max_failures=2, window_seconds=300)
    assert limiter.blocked("127.0.0.1") is False
    limiter.record_failure("127.0.0.1")
    limiter.record_failure("127.0.0.1")
    assert limiter.blocked("127.0.0.1") is True
    limiter.reset("127.0.0.1")
    assert limiter.blocked("127.0.0.1") is False
