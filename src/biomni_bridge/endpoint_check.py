from __future__ import annotations

import argparse
import sys
import time
from typing import Any

import requests

from .config import ConfigError, Settings, scrub_secret_environment
from .models import is_chat_model


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _model_ids(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        entries = payload.get("data", [])
    elif isinstance(payload, list):
        entries = payload
    else:
        return []

    result: list[str] = []
    for item in entries:
        if isinstance(item, dict):
            model_id = item.get("id")
        else:
            model_id = item if isinstance(item, str) else None
        if model_id:
            value = str(model_id).strip()
            if value and is_chat_model(value) and value not in result:
                result.append(value)
    return result


def _request_failure(label: str, response: requests.Response) -> int:
    code = response.status_code
    if code in {401, 403}:
        reason = "API key rejected or not authorized"
    elif code == 404:
        reason = "endpoint or model not found"
    elif code == 429:
        reason = "rate limited"
    elif code in {502, 503, 504}:
        reason = "upstream model gateway unavailable or timed out"
    elif code >= 500:
        reason = "upstream service error"
    else:
        reason = "request failed"
    print(f"FAIL: {label} returned HTTP {code} ({reason}).")
    return 2


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check the configured OpenAI-compatible endpoint without starting Biomni."
    )
    parser.add_argument(
        "--model",
        help="Model ID to test. Defaults to BIOMNI_MODEL, then the first chat model from GET /models.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-request timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--models-only",
        action="store_true",
        help="Only test GET /models; do not send a minimal chat completion.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.timeout <= 0:
        print("ERROR: --timeout must be greater than zero.", file=sys.stderr)
        return 2

    try:
        settings = Settings.from_env()
        settings.require_credentials()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    # Keep the validated key in Settings, but do not leave it available to any
    # subprocesses that may be started after this diagnostic.
    scrub_secret_environment()

    models_url = f"{settings.base_url}/models"
    print(f"Checking model list: {models_url}")
    try:
        response = requests.get(
            models_url,
            headers=_headers(settings.api_key),
            timeout=args.timeout,
        )
    except requests.Timeout:
        print(f"FAIL: GET /models exceeded {args.timeout:g} seconds.")
        return 2
    except requests.RequestException as exc:
        print(f"FAIL: GET /models could not connect ({type(exc).__name__}).")
        return 2

    if not response.ok:
        return _request_failure("GET /models", response)

    try:
        models = _model_ids(response.json())
    except ValueError:
        print("FAIL: GET /models returned HTTP 200 but the body was not JSON.")
        return 2

    print(f"OK: GET /models returned HTTP {response.status_code}; {len(models)} chat model(s) found.")
    if args.models_only:
        return 0

    model = (args.model or settings.default_model or "").strip()
    if not model and models:
        model = models[0]
    if not model:
        print("FAIL: no chat model is available. Set BIOMNI_MODEL or pass --model.")
        return 2

    chat_url = f"{settings.base_url}/chat/completions"
    print(f"Checking chat completion with model: {model}")
    started = time.monotonic()
    try:
        response = requests.post(
            chat_url,
            headers=_headers(settings.api_key),
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply with only OK."}],
                "max_tokens": 8,
                "temperature": 0,
                "stream": False,
            },
            timeout=args.timeout,
        )
    except requests.Timeout:
        elapsed = time.monotonic() - started
        print(
            f"FAIL: POST /chat/completions exceeded {args.timeout:g} seconds "
            f"(waited {elapsed:.1f}s). The route may be reachable, but the model backend is too slow or unavailable."
        )
        return 3
    except requests.RequestException as exc:
        print(f"FAIL: POST /chat/completions could not connect ({type(exc).__name__}).")
        return 3

    elapsed = time.monotonic() - started
    if not response.ok:
        _request_failure("POST /chat/completions", response)
        print(f"Model test failed after {elapsed:.1f}s.")
        return 3

    try:
        payload = response.json()
    except ValueError:
        print("FAIL: POST /chat/completions returned HTTP 200 but the body was not JSON.")
        return 3

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if not isinstance(choices, list) or not choices:
        print("FAIL: chat endpoint returned HTTP 200 but no OpenAI-compatible choices array.")
        return 3

    print(f"OK: POST /chat/completions returned HTTP {response.status_code} in {elapsed:.1f}s.")
    print("Endpoint, API key, selected model, and chat route are working.")
    print(
        "Note: this is a minimal 8-token health probe. Biomni sends much larger "
        "prompts, so a large/slow model can still hit gateway timeouts during agent runs."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
