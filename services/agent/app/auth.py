"""Core access-token authentication for the Agent HTTP API."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import jwt
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app.config import Settings

ACCESS_TOKEN_TYPE = "access"
CLOCK_LEEWAY_SECONDS = 30


class AuthenticationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    session_id: str


class CoreTokenAuthentication:
    def __init__(
        self,
        *,
        public_key: str,
        issuer: str,
        audience: str,
    ) -> None:
        if not public_key.strip():
            raise RuntimeError("Agent JWT public key is empty")
        if not issuer.strip() or not audience.strip():
            raise RuntimeError("Agent JWT issuer and audience are required")
        self.public_key = public_key
        self.issuer = issuer
        self.audience = audience

    @classmethod
    def from_settings(cls, settings: Settings) -> "CoreTokenAuthentication":
        key_path = Path(settings.jwt_public_key_file).expanduser()
        if not key_path.is_absolute():
            key_path = (Path.cwd() / key_path).resolve()
        if not key_path.is_file():
            raise RuntimeError(f"Agent JWT public key file does not exist: {key_path}")
        return cls(
            public_key=key_path.read_text(encoding="utf-8"),
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
        )

    def authenticate(self, authorization: str | None) -> AuthenticatedUser:
        parts = (authorization or "").strip().split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
            raise AuthenticationError("authentication required")

        try:
            claims = jwt.decode(
                parts[1],
                self.public_key,
                algorithms=["RS256", "RS384", "RS512"],
                issuer=self.issuer,
                audience=self.audience,
                leeway=CLOCK_LEEWAY_SECONDS,
                options={
                    "require": ["sub", "exp", "iat", "nbf"],
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError("invalid access token") from exc

        user_id = str(claims.get("sub") or "").strip()
        session_id = str(claims.get("sid") or "").strip()
        if claims.get("typ") != ACCESS_TOKEN_TYPE:
            raise AuthenticationError("invalid token type")
        try:
            uuid.UUID(user_id)
        except ValueError as exc:
            raise AuthenticationError("invalid token subject") from exc
        if not session_id:
            raise AuthenticationError("invalid token session")
        return AuthenticatedUser(user_id=user_id, session_id=session_id)


def install_authentication_error_handler(application: FastAPI) -> None:
    @application.exception_handler(AuthenticationError)
    async def _authentication_error(_request: Request, _exc: AuthenticationError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={
                "code": "AUTH_UNAUTHENTICATED",
                "message": "authentication required",
                "retryable": False,
            },
        )


def current_user(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthenticatedUser:
    authentication = getattr(request.app.state, "agent_authentication", None)
    if authentication is None:
        raise RuntimeError("Agent authentication is not configured")
    return authentication.authenticate(authorization)


def current_user_id(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
) -> str:
    return user.user_id
