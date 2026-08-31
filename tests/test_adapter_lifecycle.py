import sys
import types
from pathlib import Path

from biomni_bridge.adapter import BiomniAdapter
from biomni_bridge.config import Settings


class FakeConfig:
    path = "./data"
    llm = ""
    source = None
    base_url = None
    api_key = None
    timeout_seconds = 600
    temperature = 0.7
    use_tool_retriever = True
    commercial_mode = False


class CountingA1:
    builds: list[str] = []

    def __init__(self, **kwargs):
        self.__class__.builds.append(kwargs["llm"])


def _install(monkeypatch):
    CountingA1.builds = []
    biomni_module = types.ModuleType("biomni")
    config_module = types.ModuleType("biomni.config")
    config_module.default_config = FakeConfig()
    agent_module = types.ModuleType("biomni.agent")
    agent_module.A1 = CountingA1
    monkeypatch.setitem(sys.modules, "biomni", biomni_module)
    monkeypatch.setitem(sys.modules, "biomni.config", config_module)
    monkeypatch.setitem(sys.modules, "biomni.agent", agent_module)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        base_url="https://example.test/api",
        api_key="secret",
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
    )


def test_adapter_construction_is_lazy(monkeypatch, tmp_path):
    _install(monkeypatch)
    BiomniAdapter(_settings(tmp_path), "model-a")
    assert CountingA1.builds == []


def test_agent_is_built_once_and_reused(monkeypatch, tmp_path):
    _install(monkeypatch)
    adapter = BiomniAdapter(_settings(tmp_path), "model-a")
    first = adapter._ensure_agent()
    second = adapter._ensure_agent()
    assert first is second
    assert CountingA1.builds == ["model-a"]


def test_model_change_rebuilds_lazily(monkeypatch, tmp_path):
    _install(monkeypatch)
    adapter = BiomniAdapter(_settings(tmp_path), "model-a")
    adapter._ensure_agent()

    status = adapter.set_model("model-b")
    assert "next task" in status
    assert CountingA1.builds == ["model-a"]

    adapter._ensure_agent()
    assert CountingA1.builds == ["model-a", "model-b"]


def test_selecting_same_model_does_not_rebuild(monkeypatch, tmp_path):
    _install(monkeypatch)
    adapter = BiomniAdapter(_settings(tmp_path), "model-a")
    adapter._ensure_agent()
    adapter.set_model("model-a")
    adapter._ensure_agent()
    assert CountingA1.builds == ["model-a"]


def test_reset_forces_fresh_agent(monkeypatch, tmp_path):
    _install(monkeypatch)
    adapter = BiomniAdapter(_settings(tmp_path), "model-a")
    adapter._ensure_agent()
    adapter.reset()
    adapter._ensure_agent()
    assert CountingA1.builds == ["model-a", "model-a"]
