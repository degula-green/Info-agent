from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import AuthenticationError, CoreTokenAuthentication
from app.config import Settings

ISSUER = "info-agent-core"
AUDIENCE = "info-agent-api"
USER_ID = "7d0779ab-9ea4-409e-a51c-842b5b9fb875"


def _key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_key, public_pem


def _token(
    private_key,
    *,
    user_id: str = USER_ID,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    token_type: str = "access",
    expires_delta: timedelta = timedelta(minutes=15),
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": user_id,
            "sid": "session-1",
            "token_type": token_type,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "nbf": now,
            "exp": now + expires_delta,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "v1"},
    )


def test_verifies_core_access_token_and_returns_subject() -> None:
    private_key, public_key = _key_pair()
    authentication = CoreTokenAuthentication(
        public_key=public_key,
        issuer=ISSUER,
        audience=AUDIENCE,
    )

    user = authentication.authenticate(f"Bearer {_token(private_key)}")

    assert user.user_id == USER_ID
    assert user.session_id == "session-1"


def test_loads_public_key_from_configured_pem_file(tmp_path) -> None:
    private_key, public_key = _key_pair()
    key_path = tmp_path / "jwt-public.pem"
    key_path.write_text(public_key, encoding="utf-8")
    authentication = CoreTokenAuthentication.from_settings(
        Settings(
            jwt_public_key_file=str(key_path),
            jwt_issuer=ISSUER,
            jwt_audience=AUDIENCE,
        )
    )

    user = authentication.authenticate(f"Bearer {_token(private_key)}")

    assert user.user_id == USER_ID


@pytest.mark.parametrize(
    "authorization",
    [None, "", "token-without-scheme", "Basic abc", "Bearer "],
)
def test_rejects_missing_or_malformed_authorization(authorization: str | None) -> None:
    _private_key, public_key = _key_pair()
    authentication = CoreTokenAuthentication(
        public_key=public_key,
        issuer=ISSUER,
        audience=AUDIENCE,
    )

    with pytest.raises(AuthenticationError):
        authentication.authenticate(authorization)


def test_rejects_expired_token() -> None:
    private_key, public_key = _key_pair()
    authentication = CoreTokenAuthentication(
        public_key=public_key,
        issuer=ISSUER,
        audience=AUDIENCE,
    )
    token = _token(private_key, expires_delta=timedelta(minutes=-5))

    with pytest.raises(AuthenticationError):
        authentication.authenticate(f"Bearer {token}")


def test_rejects_wrong_issuer_audience_and_token_type() -> None:
    private_key, public_key = _key_pair()
    authentication = CoreTokenAuthentication(
        public_key=public_key,
        issuer=ISSUER,
        audience=AUDIENCE,
    )

    with pytest.raises(AuthenticationError):
        authentication.authenticate(
            f"Bearer {_token(private_key, issuer='another-issuer')}"
        )
    with pytest.raises(AuthenticationError):
        authentication.authenticate(
            f"Bearer {_token(private_key, audience='another-audience')}"
        )
    with pytest.raises(AuthenticationError):
        authentication.authenticate(
            f"Bearer {_token(private_key, token_type='refresh')}"
        )


def test_rejects_token_signed_by_another_key() -> None:
    signer, _public_key = _key_pair()
    _trusted_key, trusted_public_key = _key_pair()
    authentication = CoreTokenAuthentication(
        public_key=trusted_public_key,
        issuer=ISSUER,
        audience=AUDIENCE,
    )

    with pytest.raises(AuthenticationError):
        authentication.authenticate(f"Bearer {_token(signer)}")
