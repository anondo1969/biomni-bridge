import sys
import types
from pathlib import Path

import pytest

from biomni_bridge.adapter import BiomniAdapter
from biomni_bridge.config import Settings


class FakeConfig:
    path = "old-path"
    llm = "old-model"
    source = "old-source"
    base_url = "old-url"
    api_key = "old-key"
    timeout_seconds = 10
    temperature = 0.1
    use_tool_retriever = False
    commercial_mode = False


class FakeA1:
    seen_global_key = "not-set"
    kwargs = None

    def __init__(self, **kwargs):
        from biomni.config import default_config

        FakeA1.seen_global_key = default_config.api_key
        FakeA1.kwargs = kwargs


class RuntimeA1(FakeA1):
    seen_stream_keys: list[str | None] = []

    def go_stream(self, prompt):
        from biomni.config import default_config

        RuntimeA1.seen_stream_keys.append(default_config.api_key)
        yield {"output": "working"}
        RuntimeA1.seen_stream_keys.append(default_config.api_key)
        yield {"output": "<solution>done</solution>"}


class FailingA1:
    def __init__(self, **kwargs):
        from biomni.config import default_config

        assert default_config.api_key is None
        raise RuntimeError("constructor failed")


class ConfigObservingA1(FakeA1):
    observations: list[tuple[str, str, str | None, str]] = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.model = kwargs["llm"]
        self.base_url = kwargs["base_url"]
        self.api_key = kwargs["api_key"]

    def go_stream(self, prompt):
        from biomni.config import default_config

        ConfigObservingA1.observations.append(
            (default_config.llm, default_config.base_url, default_config.api_key, prompt)
        )
        yield {"output": f"<solution>{self.model}</solution>"}


def _install_fake_biomni(monkeypatch, a1_type):
    biomni_module = types.ModuleType("biomni")
    config_module = types.ModuleType("biomni.config")
    config_module.default_config = FakeConfig()
    agent_module = types.ModuleType("biomni.agent")
    agent_module.A1 = a1_type
    monkeypatch.setitem(sys.modules, "biomni", biomni_module)
    monkeypatch.setitem(sys.modules, "biomni.config", config_module)
    monkeypatch.setitem(sys.modules, "biomni.agent", agent_module)
    return config_module


def _settings(tmp_path):
    return Settings(
        base_url="https://example.test/api",
        api_key="super-secret",
        data_dir=Path(tmp_path / "data"),
        output_dir=Path(tmp_path / "output"),
        temperature=0.2,
    )


def test_secret_not_in_global_config_during_agent_construction(monkeypatch, tmp_path):
    config_module = _install_fake_biomni(monkeypatch, FakeA1)
    adapter = BiomniAdapter(_settings(tmp_path), "bridge-model")

    agent = adapter._ensure_agent()
    assert FakeA1.seen_global_key is None
    assert FakeA1.kwargs["api_key"] == "super-secret"
    assert FakeA1.kwargs["source"] == "Custom"
    assert FakeA1.kwargs["expected_data_lake_files"] == []
    assert config_module.default_config.api_key is None
    assert config_module.default_config.source == "Custom"
    assert config_module.default_config.temperature == 0.2
    assert agent is adapter._agent


def test_secret_is_scoped_to_active_biomni_stream_steps(monkeypatch, tmp_path):
    config_module = _install_fake_biomni(monkeypatch, RuntimeA1)
    RuntimeA1.seen_stream_keys = []
    adapter = BiomniAdapter(_settings(tmp_path), "bridge-model")

    updates = list(adapter.stream("task"))

    assert updates[-1].final_answer == "done"
    assert RuntimeA1.seen_stream_keys == ["super-secret", "super-secret"]
    assert config_module.default_config.api_key is None


def test_failed_build_restores_nonsecret_config_and_scrubs_key(monkeypatch, tmp_path):
    config_module = _install_fake_biomni(monkeypatch, FailingA1)
    adapter = BiomniAdapter(_settings(tmp_path), "bridge-model")

    with pytest.raises(RuntimeError, match="constructor failed"):
        adapter._ensure_agent()

    config = config_module.default_config
    assert config.api_key is None
    assert config.llm == "old-model"
    assert config.base_url == "old-url"


def test_each_adapter_reapplies_its_nonsecret_global_config(monkeypatch, tmp_path):
    config_module = _install_fake_biomni(monkeypatch, ConfigObservingA1)
    ConfigObservingA1.observations = []

    settings_a = Settings(
        base_url="https://a.example.test/api",
        api_key="key-a",
        data_dir=Path(tmp_path / "data-a"),
        output_dir=Path(tmp_path / "output-a"),
    )
    settings_b = Settings(
        base_url="https://b.example.test/api",
        api_key="key-b",
        data_dir=Path(tmp_path / "data-b"),
        output_dir=Path(tmp_path / "output-b"),
    )
    adapter_a = BiomniAdapter(settings_a, "model-a")
    adapter_b = BiomniAdapter(settings_b, "model-b")

    # Build both first so B is the last adapter to touch Biomni's process-global
    # non-secret config. Streaming A must still restore A's own values.
    adapter_a._ensure_agent()
    adapter_b._ensure_agent()
    assert config_module.default_config.llm == "model-b"
    assert config_module.default_config.base_url == "https://b.example.test/api"

    updates_a = list(adapter_a.stream("task-a"))
    updates_b = list(adapter_b.stream("task-b"))

    assert updates_a[-1].final_answer == "model-a"
    assert updates_b[-1].final_answer == "model-b"
    assert ConfigObservingA1.observations == [
        ("model-a", "https://a.example.test/api", "key-a", "task-a"),
        ("model-b", "https://b.example.test/api", "key-b", "task-b"),
    ]
    assert config_module.default_config.api_key is None

