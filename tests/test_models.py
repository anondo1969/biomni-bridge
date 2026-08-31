from pathlib import Path

from biomni_bridge.config import Settings
from biomni_bridge.models import _parse_model_payload, resolve_model_choices


def _settings(**kwargs):
    defaults = dict(
        base_url="https://example.test/api",
        api_key="secret",
        data_dir=Path("/tmp/data"),
        output_dir=Path("/tmp/output"),
    )
    defaults.update(kwargs)
    return Settings(**defaults)


def test_parse_model_payload_filters_non_chat_and_deduplicates():
    payload = {
        "data": [
            {"id": "bridge-chat"},
            {"id": "text-embedding-large"},
            {"id": "clip-vision-model"},
            {"id": "bridge-chat"},
            {"id": "Qwen3-235B"},
        ]
    }
    assert _parse_model_payload(payload) == ["bridge-chat", "Qwen3-235B"]


def test_fallback_models_used_when_discovery_disabled():
    settings = _settings(
        discover_models=False,
        fallback_models=("embed-model", "bridge-a", "bridge-b"),
        default_model="bridge-b",
    )
    info = resolve_model_choices(settings)
    assert info.choices == ("bridge-a", "bridge-b")
    assert info.selected == "bridge-b"


def test_no_hardcoded_public_model_when_discovery_is_unavailable():
    info = resolve_model_choices(_settings(discover_models=False))
    assert info.choices == ()
    assert info.selected is None


def test_validate_model_connection_populates_models(monkeypatch):
    from biomni_bridge.models import validate_model_connection

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"data": [{"id": "bridge-a"}, {"id": "text-embedding-large"}, {"id": "bridge-b"}]}

    monkeypatch.setattr("biomni_bridge.models.requests.get", lambda *args, **kwargs: Response())
    info = validate_model_connection(_settings())
    assert info.choices == ("bridge-a", "bridge-b")
    assert info.selected == "bridge-a"
    assert info.status.startswith("Connected.")


def test_validate_model_connection_rejects_redirect(monkeypatch):
    import pytest

    from biomni_bridge.models import ModelConnectionError, validate_model_connection

    class Response:
        status_code = 302
        headers = {"location": "http://127.0.0.1/internal"}

    monkeypatch.setattr("biomni_bridge.models.requests.get", lambda *args, **kwargs: Response())
    with pytest.raises(ModelConnectionError, match="redirected"):
        validate_model_connection(_settings())
