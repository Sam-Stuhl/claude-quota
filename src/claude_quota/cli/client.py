"""Thin HTTP client the CLI uses to talk to the daemon."""

from __future__ import annotations

import httpx

from .. import config


class DaemonError(RuntimeError):
    """Raised when the daemon is unreachable or returns an error."""


def _headers() -> dict:
    tok = config.client_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _get(path: str, params: dict | None = None, timeout: float = 3.0) -> dict:
    url = f"{config.client_base_url()}{path}"
    try:
        resp = httpx.get(url, params=params, headers=_headers(), timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise DaemonError(str(e)) from e


def is_up(timeout: float = 1.0) -> bool:
    try:
        httpx.get(
            f"{config.client_base_url()}/api/health", headers=_headers(), timeout=timeout
        ).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def summary() -> dict:
    return _get("/api/summary")


def sessions() -> dict:
    return _get("/api/sessions")


def breakdown(by: str = "model", window: str = "5h") -> dict:
    return _get("/api/breakdown", {"by": by, "window": window})


def history(days: int = 14) -> dict:
    return _get("/api/history", {"days": days})


def health() -> dict:
    return _get("/api/health")
