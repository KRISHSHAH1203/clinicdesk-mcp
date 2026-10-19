"""Signed caller tokens and role-based access control.

A token is base64url(JSON claims) + "." + HMAC-SHA256 signature. Claims carry the
tenant (clinic), the role and an expiry. Every tool checks a scope before touching data,
and every query is filtered by the caller's tenant.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

ROLE_SCOPES: dict[str, set[str]] = {
    "front_desk": {"patients:read", "slots:read", "appointments:read", "appointments:write"},
    "clinician": {"patients:read", "slots:read", "appointments:read"},
    "agent": {"slots:read", "appointments:read", "appointments:write"},
}


class AuthError(Exception):
    """Raised when a token is missing, invalid, expired or lacks a scope."""


@dataclass(frozen=True)
class Caller:
    subject: str
    tenant: str
    role: str
    provider: str | None = None  # attribute used for ABAC: which provider a clinician is

    @property
    def scopes(self) -> set[str]:
        return ROLE_SCOPES.get(self.role, set())

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise AuthError(f"role '{self.role}' lacks scope '{scope}'")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(secret: str, subject: str, tenant: str, role: str, ttl_seconds: int = 3600,
                now: float | None = None, provider: str | None = None) -> str:
    if role not in ROLE_SCOPES:
        raise ValueError(f"unknown role: {role}")
    issued = int(now if now is not None else time.time())
    claims = {"sub": subject, "tenant": tenant, "role": role, "exp": issued + ttl_seconds}
    if provider:
        claims["provider"] = provider
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    sig = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(secret: str, token: str, now: float | None = None) -> Caller:
    try:
        body, sig = token.split(".")
    except ValueError as exc:
        raise AuthError("malformed token") from exc
    expected = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise AuthError("bad signature")
    claims = json.loads(_unb64(body))
    if claims["exp"] < (now if now is not None else time.time()):
        raise AuthError("token expired")
    if claims["role"] not in ROLE_SCOPES:
        raise AuthError("unknown role")
    return Caller(subject=claims["sub"], tenant=claims["tenant"], role=claims["role"],
                  provider=claims.get("provider"))


def can_view_appointment(caller: Caller, appointment: dict) -> bool:
    """Attribute-based rule on top of RBAC: a clinician sees only their own appointments."""
    if caller.role == "clinician":
        return caller.provider is not None and appointment["provider"] == caller.provider
    return True
