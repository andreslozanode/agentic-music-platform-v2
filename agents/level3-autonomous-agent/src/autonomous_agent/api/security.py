"""Authentication (API keys or OIDC JWT), RBAC and HTTP hardening middleware."""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import jwt
from fastapi import HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from autonomous_agent.config import Settings

ROLES = frozenset({"reader", "operator", "approver", "auditor"})


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    method: str = "api_key"


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class APIKeyAuthenticator:
    """Keys are configured as ``name:role1|role2:sha256hex``; plaintext keys are never stored."""

    def __init__(self, spec: str) -> None:
        self._entries: list[tuple[str, frozenset[str], str]] = []
        for item in filter(None, (s.strip() for s in spec.split(","))):
            name, roles, digest = item.split(":")
            role_set = frozenset(roles.split("|"))
            if not role_set <= ROLES or len(digest) != 64:
                raise ValueError(f"invalid API key entry for '{name}'")
            self._entries.append((name, role_set, digest.lower()))

    def authenticate(self, presented: str) -> Principal | None:
        digest = hash_key(presented)
        match: Principal | None = None
        for name, roles, expected in self._entries:  # constant-time over all entries
            if hmac.compare_digest(digest, expected):
                match = Principal(name, roles, "api_key")
        return match


class JWTAuthenticator:
    def __init__(self, jwks_url: str, issuer: str, audience: str) -> None:
        self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
        self.issuer, self.audience = issuer, audience

    def authenticate(self, token: str) -> Principal | None:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.PyJWTError:
            return None
        raw_roles = claims.get("roles") or str(claims.get("scope", "")).split()
        roles = frozenset(r.removeprefix("agent.") for r in raw_roles) & ROLES
        return Principal(str(claims["sub"]), roles, "jwt")


class Authenticator:
    def __init__(self, settings: Settings) -> None:
        self.api_keys = (
            APIKeyAuthenticator(settings.api_keys.get_secret_value()) if settings.api_keys else None
        )
        self.jwt = (
            JWTAuthenticator(
                settings.jwt_jwks_url, settings.jwt_issuer or "", settings.jwt_audience or ""
            )
            if settings.jwt_jwks_url
            else None
        )
        self.open_dev_mode = settings.environment in {"dev", "ci"} and not (
            self.api_keys or self.jwt
        )

    def __call__(self, request: Request) -> Principal:
        header = request.headers.get("authorization", "")
        api_key = request.headers.get("x-api-key")
        principal: Principal | None = None
        if api_key and self.api_keys:
            principal = self.api_keys.authenticate(api_key)
        elif header.lower().startswith("bearer ") and self.jwt:
            principal = self.jwt.authenticate(header[7:].strip())
        elif self.open_dev_mode:
            principal = Principal("dev-user", ROLES, "none")
        if principal is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        request.state.principal = principal
        return principal


def require_role(role: str) -> Callable[[Request], Principal]:
    def dependency(request: Request) -> Principal:
        principal: Principal | None = getattr(request.state, "principal", None)
        if principal is None:
            authenticator: Authenticator = request.app.state.authenticator
            principal = authenticator(request)
        if role not in principal.roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role '{role}' required")
        return principal

    return dependency


SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}

Next = Callable[[Request], Awaitable[Response]]


class HardeningMiddleware(BaseHTTPMiddleware):
    """Request id, body size limit, per-client rate limit and security headers."""

    def __init__(self, app: ASGIApp, *, max_body: int, per_minute: int) -> None:
        super().__init__(app)
        self.max_body = max_body
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _client_key(self, request: Request) -> str:
        cred = request.headers.get("x-api-key") or request.headers.get("authorization") or ""
        if cred:
            return "cred:" + hash_key(cred)[:16]
        return "ip:" + (request.client.host if request.client else "unknown")

    def _limited(self, key: str) -> bool:
        now = time.monotonic()
        window = self._hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return True
        window.append(now)
        return False

    async def dispatch(self, request: Request, call_next: Next) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        if len(request_id) > 64 or not request_id.replace("-", "").isalnum():
            request_id = uuid.uuid4().hex
        response: Response
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > self.max_body):
            response = JSONResponse({"detail": "request body too large"}, status_code=413)
        elif request.url.path.startswith("/v1/") and self._limited(self._client_key(request)):
            response = JSONResponse(
                {"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"}
            )
        else:
            body = await request.body()
            if len(body) > self.max_body:
                response = JSONResponse({"detail": "request body too large"}, status_code=413)
            else:
                response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers[k] = v
        response.headers["X-Request-ID"] = request_id
        return response
