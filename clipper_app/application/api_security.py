from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "testserver")
DEFAULT_ALLOWED_ORIGINS = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


def normalize_actor(value: str | None) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9@._:+-]+", "-", str(value or "").strip()).strip("-")
    return normalized[:120] or "local-operator"


@dataclass(frozen=True)
class ApiSecuritySettings:
    token: str | None
    actor: str
    desktop: bool
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]

    @classmethod
    def from_environment(cls) -> "ApiSecuritySettings":
        token = os.getenv("CLIPPER_CONTROL_TOKEN", "").strip() or None
        desktop = os.getenv("CLIPPER_DESKTOP", "").strip().casefold() in {"1", "true", "yes"}
        return cls(
            token=token,
            actor=normalize_actor(os.getenv("CLIPPER_CONTROL_ACTOR")),
            desktop=desktop,
            allowed_hosts=tuple(dict.fromkeys((*DEFAULT_ALLOWED_HOSTS, *_csv_env("CLIPPER_ALLOWED_HOSTS")))),
            allowed_origins=tuple(dict.fromkeys((*DEFAULT_ALLOWED_ORIGINS, *_csv_env("CLIPPER_ALLOWED_ORIGINS")))),
        )

    def authorize(self, authorization: str | None) -> bool:
        if self.token is None:
            return False
        scheme, _, supplied = str(authorization or "").partition(" ")
        return scheme.casefold() == "bearer" and bool(supplied) and hmac.compare_digest(supplied, self.token)


def is_sensitive_read(path: str) -> bool:
    normalized = str(path or "").rstrip("/") or "/"
    if normalized in {"/api/artifacts", "/api/logs", "/api/settings", "/api/settings/effective"}:
        return True
    if normalized.startswith("/api/control/jobs/"):
        return True
    if normalized.startswith("/api/modules/") and normalized not in {
        "/api/modules/library",
        "/api/modules/readiness",
    }:
        return True
    return False


def requires_control_auth(method: str, path: str) -> bool:
    return str(method).upper() not in SAFE_METHODS or is_sensitive_read(path)


def origin_allowed(origin: str | None, request_host: str, settings: ApiSecuritySettings) -> bool:
    if origin is None or not origin.strip():
        return True
    if origin.strip().casefold() == "null":
        return False
    if origin.rstrip("/") in settings.allowed_origins:
        return True
    try:
        parsed = urlsplit(origin)
        host = parsed.netloc.casefold()
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and host == request_host.casefold()
