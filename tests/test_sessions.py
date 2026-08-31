from pathlib import Path

from biomni_bridge.config import Settings
from biomni_bridge.sessions import SessionRegistry


class FakeAdapter:
    def __init__(self, settings, model):
        self.settings = settings
        self.model = model
        self.reset_calls = 0

    def reset(self):
        self.reset_calls += 1


def _settings(key: str) -> Settings:
    return Settings(
        base_url="https://models.example.org/v1",
        api_key=key,
        data_dir=Path("/tmp/data"),
        output_dir=Path("/tmp/output"),
    )


def test_registry_isolates_credentials_between_sessions(monkeypatch):
    monkeypatch.setattr("biomni_bridge.sessions.BiomniAdapter", FakeAdapter)
    registry = SessionRegistry(ttl_seconds=3600)

    a = registry.connect("session-a", _settings("key-a"), "model-a")
    b = registry.connect("session-b", _settings("key-b"), "model-b")

    assert registry.require("session-a") is a
    assert registry.require("session-b") is b
    assert a.settings.api_key == "key-a"
    assert b.settings.api_key == "key-b"
    assert len(registry) == 2


def test_disconnect_releases_session_adapter(monkeypatch):
    monkeypatch.setattr("biomni_bridge.sessions.BiomniAdapter", FakeAdapter)
    registry = SessionRegistry(ttl_seconds=3600)
    runtime = registry.connect("session-a", _settings("key-a"), "model-a")

    registry.remove("session-a")

    assert runtime.adapter.reset_calls == 1
    assert registry.get("session-a") is None
    assert len(registry) == 0


def test_reconnect_replaces_previous_runtime(monkeypatch):
    monkeypatch.setattr("biomni_bridge.sessions.BiomniAdapter", FakeAdapter)
    registry = SessionRegistry(ttl_seconds=3600)
    old = registry.connect("session-a", _settings("key-a"), "model-a")
    new = registry.connect("session-a", _settings("key-b"), "model-b")

    assert old.adapter.reset_calls == 1
    assert registry.require("session-a") is new
    assert new.settings.api_key == "key-b"


def test_ui_sessions_get_separate_output_directories(monkeypatch, tmp_path):
    monkeypatch.setattr("biomni_bridge.sessions.BiomniAdapter", FakeAdapter)
    registry = SessionRegistry(ttl_seconds=3600)
    base = Settings(
        base_url="https://models.example.org/v1",
        api_key="key",
        output_dir=tmp_path,
        credential_mode="ui",
    )

    a = registry.connect("session-a", base, "model")
    b = registry.connect("session-b", base, "model")

    assert a.settings.output_dir != b.settings.output_dir
    assert a.settings.output_dir.parent == tmp_path / "sessions"
    assert b.settings.output_dir.parent == tmp_path / "sessions"
    assert a.settings.output_dir.is_dir()
    assert b.settings.output_dir.is_dir()
