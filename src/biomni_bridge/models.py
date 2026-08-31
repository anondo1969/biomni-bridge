from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import requests

from .config import Settings

log = logging.getLogger(__name__)
REQUEST_TIMEOUT_SECONDS = 10
NON_CHAT_MARKERS = (
    "bge",
    "embed",
    "rerank",
    "e5-",
    "gte-",
    "nomic",
    "minilm",
    "sentence-transformers",
    "whisper",
    "tts",
    "clip",
)


@dataclass(frozen=True, slots=True)
class ModelChoices:
    choices: tuple[str, ...]
    selected: str | None
    status: str


class ModelConnectionError(RuntimeError):
    """Raised when an explicitly requested endpoint/key validation fails."""


def is_chat_model(model_id: str) -> bool:
    lowered = model_id.lower()
    return not any(marker in lowered for marker in NON_CHAT_MARKERS)


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _parse_model_payload(payload: Any) -> list[str]:
    entries: Any
    if isinstance(payload, dict):
        entries = payload.get("data", [])
    elif isinstance(payload, list):
        entries = payload
    else:
        raise ValueError("/models response must be a JSON object or list")

    result: list[str] = []
    for item in entries:
        if isinstance(item, dict) and item.get("id"):
            result.append(str(item["id"]).strip())
        elif isinstance(item, str):
            result.append(item.strip())
    return _dedupe(model for model in result if model and is_chat_model(model))


def _fetch_models(settings: Settings) -> list[str]:
    if not settings.base_url or not settings.api_key:
        raise ModelConnectionError("Connect an API endpoint and key first")

    url = f"{settings.base_url}/models"
    try:
        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise ModelConnectionError(f"Could not reach {settings.base_url}") from exc

    if 300 <= response.status_code < 400:
        raise ModelConnectionError("The model endpoint redirected GET /models; redirects are not accepted")
    if response.status_code in {401, 403}:
        raise ModelConnectionError("The endpoint rejected the API key")
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        raise ModelConnectionError(f"GET /models returned HTTP {response.status_code}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        content_type = response.headers.get("content-type", "unknown content type")
        raise ModelConnectionError(f"GET /models did not return JSON ({content_type})") from exc
    return _parse_model_payload(payload)


def discover_models(settings: Settings) -> list[str]:
    """Return usable model IDs from the configured OpenAI-compatible endpoint.

    Discovery is best effort. Some gateways intentionally do not expose
    ``GET /models``; callers can fall back to BIOMNI_MODEL/BIOMNI_MODELS.
    """
    if not settings.discover_models or not settings.has_credentials:
        return []

    try:
        return _fetch_models(settings)
    except ModelConnectionError as exc:
        log.warning("Model discovery unavailable at %s: %s", settings.base_url, type(exc).__name__)
        return []


def _choices_from_models(settings: Settings, discovered: list[str], *, connected: bool) -> ModelChoices:
    fallback = [model for model in settings.fallback_models if is_chat_model(model)]

    if discovered:
        choices = discovered
        prefix = "Connected. " if connected else ""
        status = f"{prefix}Found {len(discovered)} model(s) at the configured endpoint."
    elif fallback:
        choices = fallback
        status = "Connected; using BIOMNI_MODELS because no model list is available."
    elif settings.default_model:
        choices = [settings.default_model]
        status = "Connected; using BIOMNI_MODEL because no model list is available."
    else:
        choices = []
        status = "Connected, but no model list is available. Type a model ID before running a task."

    if settings.default_model and settings.default_model not in choices:
        choices.insert(0, settings.default_model)

    selected = settings.default_model or (choices[0] if choices else None)
    return ModelChoices(tuple(_dedupe(choices)), selected, status)


def validate_model_connection(settings: Settings) -> ModelChoices:
    """Validate UI-provided endpoint/key with GET /models and return choices.

    This gives the user immediate feedback without spending tokens on a chat completion.
    Redirects are rejected so endpoint validation cannot silently change targets.
    """
    discovered = _fetch_models(settings)
    return _choices_from_models(settings, discovered, connected=True)


def resolve_model_choices(settings: Settings) -> ModelChoices:
    discovered = discover_models(settings)
    return _choices_from_models(settings, discovered, connected=settings.has_credentials)
