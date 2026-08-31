from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_HTML_MARKERS = ("<!doctype html", "<html", "<head", "<body")
_TIMEOUT_NAMES = {"APITimeoutError", "ReadTimeout", "ConnectTimeout", "Timeout", "TimeoutError"}
_CONNECTION_NAMES = {"APIConnectionError", "ConnectionError", "ConnectError"}


@dataclass(frozen=True, slots=True)
class UserFacingError:
    message: str
    status: str


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    if isinstance(value, int):
        return value

    response: Any = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _safe_exception_text(exc: BaseException, secret: str = "", *, limit: int = 500) -> str:
    raw = str(exc).strip()
    if secret:
        raw = raw.replace(secret, "***")

    lowered = raw.lower()
    if any(marker in lowered for marker in _HTML_MARKERS):
        status = _status_code(exc)
        return f"{type(exc).__name__} (HTTP {status})" if status else type(exc).__name__

    compact = re.sub(r"\s+", " ", raw)
    if not compact:
        compact = type(exc).__name__
    if len(compact) > limit:
        compact = compact[: limit - 1].rstrip() + "..."
    return f"{type(exc).__name__}: {compact}"


def describe_model_error(exc: BaseException, *, base_url: str, secret: str = "") -> UserFacingError:
    """Convert provider/network failures into concise messages safe for the UI."""
    status = _status_code(exc)
    endpoint = base_url.rstrip("/")

    if status == 400 and "roles must alternate" in str(exc).lower():
        return UserFacingError(
            message=(
                "The model gateway rejected Biomni's multi-step message history because it requires "
                "strict user/assistant role alternation. This wrapper includes a Biomni 0.0.8 "
                "observation-role compatibility shim; if this error still appears, report it as a "
                "bridge compatibility bug."
            ),
            status="Gateway message-role compatibility error (HTTP 400)",
        )

    if status in {502, 503, 504}:
        return UserFacingError(
            message=(
                f"The model gateway at {endpoint} returned HTTP {status}. "
                "The local Biomni UI is running, but the upstream model service is unavailable "
                "or took too long to respond. Retry later or select another configured model."
            ),
            status=f"Upstream gateway error (HTTP {status})",
        )

    if status in {401, 403}:
        return UserFacingError(
            message=(
                f"The model gateway at {endpoint} rejected the request with HTTP {status}. "
                "Check that your API key is valid and authorized for the selected model."
            ),
            status=f"Authentication/authorization failed (HTTP {status})",
        )

    if status == 404:
        return UserFacingError(
            message=(
                f"The model gateway at {endpoint} returned HTTP 404. Check the API prefix and selected model ID."
            ),
            status="Endpoint or model not found (HTTP 404)",
        )

    if status == 429:
        return UserFacingError(
            message=(
                f"The model gateway at {endpoint} returned HTTP 429. The service is rate-limiting this request; "
                "retry after a short delay."
            ),
            status="Rate limited (HTTP 429)",
        )

    if status is not None and status >= 500:
        return UserFacingError(
            message=(
                f"The model gateway at {endpoint} returned HTTP {status}. "
                "The local application is healthy, but the upstream model request failed."
            ),
            status=f"Upstream service error (HTTP {status})",
        )

    name = type(exc).__name__
    if name in _TIMEOUT_NAMES:
        return UserFacingError(
            message=(
                f"Timed out while waiting for the model gateway at {endpoint}. "
                "Check service availability or try another configured model."
            ),
            status="Model request timed out",
        )

    if name in _CONNECTION_NAMES:
        return UserFacingError(
            message=(
                f"Could not connect to the model gateway at {endpoint}. "
                "Check network/VPN access and the configured API URL."
            ),
            status="Cannot reach model gateway",
        )

    return UserFacingError(
        message=_safe_exception_text(exc, secret),
        status="Task failed",
    )


def safe_exception_text(exc: BaseException, secret: str = "") -> str:
    """Return a bounded, secret-scrubbed exception string without raw HTML."""
    return _safe_exception_text(exc, secret)
