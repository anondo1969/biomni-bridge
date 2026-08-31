import sys
import threading
import types
from pathlib import Path

from biomni_bridge.config import Settings
from biomni_bridge.sessions import SessionRegistry


class FakeConfig:
    path = "neutral-path"
    llm = "neutral-model"
    source = "neutral-source"
    base_url = "https://neutral.example.test/api"
    api_key = None
    timeout_seconds = 10
    temperature = 0.1
    use_tool_retriever = False
    commercial_mode = False


class InstrumentedRLock:
    """RLock wrapper that proves B reached the lock while A still held it."""

    def __init__(self):
        self._lock = threading.RLock()
        self.b_attempted_acquire = threading.Event()

    def __enter__(self):
        if threading.current_thread().name == "session-b-disconnect":
            self.b_attempted_acquire.set()
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._lock.release()
        return False


class BlockingToolA1:
    """Fake A1 that pauses inside one graph step to simulate a tool call."""

    observations: list[tuple[str, str, str, str | None]] = []
    tool_entered = threading.Event()
    release_tool = threading.Event()

    def __init__(self, **kwargs):
        self.model = kwargs["llm"]

    def go_stream(self, prompt):
        from biomni.config import default_config

        BlockingToolA1.observations.append(
            ("tool-start", default_config.llm, default_config.base_url, default_config.api_key)
        )
        BlockingToolA1.tool_entered.set()
        if not BlockingToolA1.release_tool.wait(timeout=5):
            raise RuntimeError("test timed out waiting to release simulated tool call")

        # B's Disconnect is deliberately blocked on the process-global config
        # lock while this graph step is active. A must still see A's values.
        BlockingToolA1.observations.append(
            ("tool-end", default_config.llm, default_config.base_url, default_config.api_key)
        )
        yield {"output": "<observation>tool complete</observation>"}

        # If B's disconnect clears the global key between graph steps, A must
        # restore its complete endpoint/model/key before the next step.
        BlockingToolA1.observations.append(
            ("next-step", default_config.llm, default_config.base_url, default_config.api_key)
        )
        yield {"output": "<solution>done</solution>"}


def _install_fake_biomni(monkeypatch):
    biomni_module = types.ModuleType("biomni")
    config_module = types.ModuleType("biomni.config")
    config_module.default_config = FakeConfig()
    agent_module = types.ModuleType("biomni.agent")
    agent_module.A1 = BlockingToolA1
    monkeypatch.setitem(sys.modules, "biomni", biomni_module)
    monkeypatch.setitem(sys.modules, "biomni.config", config_module)
    monkeypatch.setitem(sys.modules, "biomni.agent", agent_module)
    return config_module


def _settings(tmp_path: Path, name: str, key: str) -> Settings:
    return Settings(
        base_url=f"https://{name}.example.test/api",
        api_key=key,
        data_dir=tmp_path / f"data-{name}",
        output_dir=tmp_path / "output",
        credential_mode="ui",
    )


def test_connect_and_disconnect_of_b_cannot_contaminate_active_a_tool_step(monkeypatch, tmp_path):
    """Regression for the highest-risk multi-session credential race.

    Exact scenario protected here:

    1. A connects with endpoint/key A.
    2. B connects with endpoint/key B.
    3. A starts a graph step that simulates a database/literature tool reading
       Biomni's process-global ``default_config``.
    4. B disconnects while A is still inside that tool step.
    5. A must see only A's endpoint/model/key during that step and must restore
       A's values again before the next step.

    A Connect/B Connect must not mutate ``default_config`` at all because agent
    construction is lazy. B Disconnect must wait for A's global-config lock and
    therefore cannot erase or replace A's credentials mid-step.
    """

    config_module = _install_fake_biomni(monkeypatch)
    guarded_lock = InstrumentedRLock()
    monkeypatch.setattr("biomni_bridge.adapter._BIOMNI_GLOBAL_CONFIG_LOCK", guarded_lock)

    BlockingToolA1.observations = []
    BlockingToolA1.tool_entered = threading.Event()
    BlockingToolA1.release_tool = threading.Event()

    registry = SessionRegistry(ttl_seconds=3600)
    runtime_a = registry.connect("session-a", _settings(tmp_path, "a", "key-a"), "model-a")
    registry.connect("session-b", _settings(tmp_path, "b", "key-b"), "model-b")

    # Merely connecting either browser must not touch Biomni's global provider
    # configuration. The lazy adapters have not been built or run yet.
    assert config_module.default_config.llm == "neutral-model"
    assert config_module.default_config.base_url == "https://neutral.example.test/api"
    assert config_module.default_config.api_key is None

    updates = []
    run_errors: list[BaseException] = []

    def run_a():
        try:
            updates.extend(runtime_a.adapter.stream("task-a"))
        except BaseException as exc:  # pragma: no cover - relayed below
            run_errors.append(exc)

    a_thread = threading.Thread(target=run_a, name="session-a-run")
    a_thread.start()
    assert BlockingToolA1.tool_entered.wait(timeout=5)

    # A is now inside next(iterator) while holding the process-global Biomni
    # config lock. Its simulated tool sees A's endpoint and key.
    assert config_module.default_config.llm == "model-a"
    assert config_module.default_config.base_url == "https://a.example.test/api"
    assert config_module.default_config.api_key == "key-a"

    disconnect_done = threading.Event()

    def disconnect_b():
        registry.remove("session-b")
        disconnect_done.set()

    b_thread = threading.Thread(target=disconnect_b, name="session-b-disconnect")
    b_thread.start()

    # This event is emitted immediately before B attempts to acquire the exact
    # same process-wide lock A already holds. Therefore a still-unset
    # disconnect_done here proves B is blocked on the lock, not merely delayed
    # by thread scheduling.
    assert guarded_lock.b_attempted_acquire.wait(timeout=2)
    assert not disconnect_done.is_set()
    assert config_module.default_config.llm == "model-a"
    assert config_module.default_config.base_url == "https://a.example.test/api"
    assert config_module.default_config.api_key == "key-a"

    BlockingToolA1.release_tool.set()
    a_thread.join(timeout=5)
    b_thread.join(timeout=5)

    assert not a_thread.is_alive()
    assert not b_thread.is_alive()
    assert run_errors == []
    assert disconnect_done.is_set()
    assert registry.get("session-b") is None
    assert registry.require("session-a") is runtime_a

    # B's endpoint/key must never be observed by A. Even if B's disconnect wins
    # the lock between A's yielded graph steps and clears the key, A restores
    # its own complete configuration before advancing the iterator again.
    assert BlockingToolA1.observations == [
        ("tool-start", "model-a", "https://a.example.test/api", "key-a"),
        ("tool-end", "model-a", "https://a.example.test/api", "key-a"),
        ("next-step", "model-a", "https://a.example.test/api", "key-a"),
    ]
    assert all(observation[2] != "https://b.example.test/api" for observation in BlockingToolA1.observations)
    assert all(observation[3] != "key-b" for observation in BlockingToolA1.observations)
    assert updates[-1].final_answer == "done"
    assert config_module.default_config.api_key is None
