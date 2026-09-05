from productlens.auth.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_is_salted_and_only_accepts_the_original_password():
    first = hash_password("correct-horse-battery")
    second = hash_password("correct-horse-battery")
    assert first != second
    assert verify_password("correct-horse-battery", first)
    assert not verify_password("wrong-password", first)


def test_signed_access_token_rejects_tampering_and_expiry():
    token = create_access_token(user_id="user-1", session_id="session-1", secret="test-secret", ttl_seconds=60)
    claims = decode_access_token(token, "test-secret")
    assert claims and claims["sub"] == "user-1" and claims["sid"] == "session-1"
    assert decode_access_token(token + "changed", "test-secret") is None
    expired = create_access_token(user_id="user-1", session_id="session-1", secret="test-secret", ttl_seconds=-1)
    assert decode_access_token(expired, "test-secret") is None
