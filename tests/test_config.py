from pathlib import Path

import pytest

from biomni_bridge.config import ConfigError, Settings, scrub_secret_environment


def _clear_required(monkeypatch):
    for name in (
        "BIOMNI_CUSTOM_BASE_URL",
        "BIOMNI_BASE_URL",
        "BIOMNI_CUSTOM_API_KEY",
        "BIOMNI_API_KEY",
        "BIOMNI_CREDENTIAL_MODE",
        "BIOMNI_ALLOWED_API_HOSTS",
        "BIOMNI_DATA_PATH",
        "BIOMNI_PATH",
        "BIOMNI_OUTPUT_PATH",
        "BIOMNI_EXPORT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_app_can_start_without_credentials(monkeypatch):
    _clear_required(monkeypatch)
    settings = Settings.from_env()
    assert settings.has_credentials is False
    assert settings.ui_credentials_enabled is True
    with pytest.raises(ConfigError, match="requires BIOMNI_CUSTOM_BASE_URL"):
        settings.require_credentials()


def test_local_paths_default_to_current_working_directory(monkeypatch, tmp_path):
    _clear_required(monkeypatch)
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_env()
    assert settings.data_dir == tmp_path / "data"
    assert settings.output_dir == tmp_path / "output"


def test_env_mode_still_requires_credentials(monkeypatch):
    _clear_required(monkeypatch)
    monkeypatch.setenv("BIOMNI_CREDENTIAL_MODE", "env")
    with pytest.raises(ConfigError, match="CREDENTIAL_MODE=env"):
        Settings.from_env()


def test_settings_from_canonical_env(monkeypatch):
    _clear_required(monkeypatch)
    monkeypatch.setenv("BIOMNI_CUSTOM_BASE_URL", "https://example.test/api/")
    monkeypatch.setenv("BIOMNI_CUSTOM_API_KEY", "secret")
    monkeypatch.setenv("BIOMNI_DATA_PATH", "/tmp/biomni-data")
    monkeypatch.setenv("BIOMNI_SKIP_DATA_DOWNLOAD", "false")
    monkeypatch.setenv("BIOMNI_MODELS", "model-a, model-b, model-a")
    monkeypatch.setenv("BIOMNI_DEBUG_LLM_REQUESTS", "true")
    settings = Settings.from_env()
    assert settings.base_url == "https://example.test/api"
    assert settings.api_key == "secret"
    assert settings.has_credentials is True
    assert settings.server_credentials_enabled is True
    assert settings.data_dir == Path("/tmp/biomni-data")
    assert settings.skip_data_download is False
    assert settings.fallback_models == ("model-a", "model-b")
    assert settings.debug_llm_requests is True
    assert settings.llm_stream_transport is True
    assert settings.qwen_disable_thinking is True


def test_ui_mode_discards_environment_api_key(monkeypatch):
    _clear_required(monkeypatch)
    monkeypatch.setenv("BIOMNI_CREDENTIAL_MODE", "ui")
    monkeypatch.setenv("BIOMNI_CUSTOM_BASE_URL", "https://models.example.org/v1")
    monkeypatch.setenv("BIOMNI_CUSTOM_API_KEY", "must-not-be-used")
    settings = Settings.from_env()
    assert settings.base_url == "https://models.example.org/v1"
    assert settings.api_key == ""
    assert settings.server_credentials_enabled is False
    assert settings.ui_credentials_enabled is True


def test_provider_compat_flags_can_be_disabled(monkeypatch):
    _clear_required(monkeypatch)
    monkeypatch.setenv("BIOMNI_CUSTOM_BASE_URL", "https://example.test/api")
    monkeypatch.setenv("BIOMNI_CUSTOM_API_KEY", "secret")
    monkeypatch.setenv("BIOMNI_LLM_STREAM_TRANSPORT", "false")
    monkeypatch.setenv("BIOMNI_QWEN_DISABLE_THINKING", "false")
    settings = Settings.from_env()
    assert settings.llm_stream_transport is False
    assert settings.qwen_disable_thinking is False


def test_short_aliases_are_supported_but_openai_env_is_not(monkeypatch):
    _clear_required(monkeypatch)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "wrong")
    monkeypatch.setenv("BIOMNI_BASE_URL", "https://example.test/api")
    monkeypatch.setenv("BIOMNI_API_KEY", "right")
    settings = Settings.from_env()
    assert settings.base_url == "https://example.test/api"
    assert settings.api_key == "right"


def test_base_url_rejects_endpoint_suffix(monkeypatch):
    _clear_required(monkeypatch)
    monkeypatch.setenv("BIOMNI_CUSTOM_BASE_URL", "https://example.test/api/chat/completions")
    monkeypatch.setenv("BIOMNI_CUSTOM_API_KEY", "secret")
    with pytest.raises(ConfigError, match="API prefix only"):
        Settings.from_env()


def test_base_url_rejects_embedded_credentials(monkeypatch):
    _clear_required(monkeypatch)
    monkeypatch.setenv("BIOMNI_CUSTOM_BASE_URL", "https://user:password@example.test/api")
    monkeypatch.setenv("BIOMNI_CUSTOM_API_KEY", "secret")
    with pytest.raises(ConfigError, match="embedded username/password"):
        Settings.from_env()


def test_ui_credentials_require_https_and_reject_private_ip():
    settings = Settings(credential_mode="ui")
    with pytest.raises(ConfigError, match="https"):
        settings.with_ui_credentials("http://example.org/api", "secret")
    with pytest.raises(ConfigError, match="private, loopback, or reserved"):
        settings.with_ui_credentials("https://127.0.0.1/api", "secret")


def test_ui_credentials_support_exact_host_allowlist_without_dns():
    settings = Settings(
        credential_mode="ui",
        allowed_api_hosts=("models.example.org",),
    )
    connected = settings.with_ui_credentials("https://models.example.org/v1/", "secret")
    assert connected.base_url == "https://models.example.org/v1"
    assert connected.api_key == "secret"

    with pytest.raises(ConfigError, match="not in BIOMNI_ALLOWED_API_HOSTS"):
        settings.with_ui_credentials("https://example.org/api", "secret")


def test_env_mode_can_disable_ui_credentials():
    settings = Settings(
        base_url="https://example.test/api",
        api_key="secret",
        credential_mode="env",
    )
    with pytest.raises(ConfigError, match="does not accept credentials"):
        settings.with_ui_credentials("https://models.example.org/v1", "other")


def test_settings_repr_never_contains_api_key():
    settings = Settings(base_url="https://example.test/api", api_key="super-secret")
    assert "super-secret" not in repr(settings)


def test_scrub_secret_environment_removes_only_wrapper_secrets(monkeypatch):
    monkeypatch.setenv("BIOMNI_CUSTOM_API_KEY", "secret-a")
    monkeypatch.setenv("BIOMNI_API_KEY", "secret-b")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated")

    scrub_secret_environment()

    assert "BIOMNI_CUSTOM_API_KEY" not in __import__("os").environ
    assert "BIOMNI_API_KEY" not in __import__("os").environ
    assert __import__("os").environ["OPENAI_API_KEY"] == "unrelated"


def test_wrapper_version_matches_release():
    from importlib.metadata import PackageNotFoundError, version

    from biomni_bridge import __version__

    try:
        installed_version = version("biomni-bridge")
    except PackageNotFoundError:
        # A source-only PYTHONPATH test has no installed distribution metadata.
        assert __version__ == "0+unknown"
    else:
        assert __version__ == installed_version
