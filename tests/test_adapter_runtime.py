import sys
import threading
import types
from pathlib import Path

from biomni_bridge.adapter import BiomniAdapter
from biomni_bridge.config import Settings


class FakeConfig:
    api_key = None


def _install_fake_config(monkeypatch):
    biomni_module = types.ModuleType("biomni")
    config_module = types.ModuleType("biomni.config")
    config_module.default_config = FakeConfig()
    monkeypatch.setitem(sys.modules, "biomni", biomni_module)
    monkeypatch.setitem(sys.modules, "biomni.config", config_module)
    return config_module


class FakeStreamingAgent:
    def __init__(self):
        self._conversation_state = None

    def go_stream(self, prompt):
        assert prompt == "test task"
        yield {"output": "planning"}
        yield {"output": "<solution>The result</solution>"}

    def save_conversation_history(self, filepath, save_pdf=True):
        assert save_pdf is True
        Path(filepath).write_bytes(b"%PDF-1.4\n% test\n")


class WorkerPdfAgent(FakeStreamingAgent):
    def save_conversation_history(self, filepath, save_pdf=True):
        raise AssertionError("worker-thread export must bypass SIGALRM wrapper")

    def _generate_markdown_content(self, include_images=True):
        assert include_images is True
        return "# Biomni history\n\nWorker-safe export.\n"


class FallbackAnswerAgent(FakeStreamingAgent):
    def go_stream(self, prompt):
        assert prompt == "test task"
        yield {"output": "planning"}
        self._conversation_state = {
            "messages": [
                types.SimpleNamespace(type="ai", content="Recovered final answer")
            ]
        }


def _adapter(tmp_path: Path, agent=None) -> BiomniAdapter:
    adapter = BiomniAdapter(
        Settings(
            base_url="https://example.test/api",
            api_key="secret",
            data_dir=tmp_path / "data",
            output_dir=tmp_path / "output",
        ),
        "bridge-model",
    )
    adapter._lock = threading.RLock()
    adapter._agent = agent or FakeStreamingAgent()
    adapter._built_model = "bridge-model"
    return adapter


def test_stream_extracts_public_solution(monkeypatch, tmp_path):
    _install_fake_config(monkeypatch)
    adapter = _adapter(tmp_path)
    updates = list(adapter.stream("test task"))
    assert len(updates) == 2
    assert updates[-1].final_answer == "The result"
    assert updates[-1].trace == "<solution>The result</solution>"
    assert adapter._has_completed_run is True


def test_stream_emits_state_fallback_once(monkeypatch, tmp_path):
    _install_fake_config(monkeypatch)
    adapter = _adapter(tmp_path, FallbackAnswerAgent())
    updates = list(adapter.stream("test task"))

    assert len(updates) == 2
    assert updates[0].final_answer is None
    assert updates[-1].trace == "planning"
    assert updates[-1].final_answer == "Recovered final answer"


def test_export_pdf_uses_output_directory(monkeypatch, tmp_path):
    _install_fake_config(monkeypatch)
    adapter = _adapter(tmp_path)
    list(adapter.stream("test task"))
    path = adapter.export_pdf()
    assert path.parent == tmp_path / "output"
    assert path.suffix == ".pdf"
    assert path.read_bytes().startswith(b"%PDF")


def test_worker_thread_pdf_export_bypasses_biomni_sigalrm(monkeypatch, tmp_path):
    _install_fake_config(monkeypatch)
    adapter = _adapter(tmp_path, WorkerPdfAgent())
    list(adapter.stream("test task"))

    converted_markdown_paths: list[Path] = []

    def fake_run(command, *, check, timeout):
        assert check is True
        assert timeout == 60
        assert command[1:3] == ["-c", command[2]]
        source = Path(command[-2])
        destination = Path(command[-1])
        assert "Worker-safe export." in source.read_text()
        converted_markdown_paths.append(source)
        destination.write_bytes(b"%PDF-1.4\n% worker test\n")

    monkeypatch.setattr("biomni_bridge.adapter.subprocess.run", fake_run)

    result: list[Path] = []
    errors: list[BaseException] = []

    def export_in_worker():
        try:
            result.append(adapter.export_pdf())
        except BaseException as exc:  # pragma: no cover - only used to relay thread failure
            errors.append(exc)

    worker = threading.Thread(target=export_in_worker)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert result and result[0].read_bytes().startswith(b"%PDF")
    assert converted_markdown_paths
    assert all(not path.exists() for path in converted_markdown_paths)


def test_export_requires_completed_run(tmp_path):
    adapter = _adapter(tmp_path)
    try:
        adapter.export_pdf()
    except ValueError as exc:
        assert "Run a task" in str(exc)
    else:
        raise AssertionError("export_pdf should require a completed run")


def test_worker_thread_pdf_timeout_is_reported(monkeypatch, tmp_path):
    import subprocess

    adapter = _adapter(tmp_path, WorkerPdfAgent())
    adapter._has_completed_run = True

    def fake_timeout(command, *, check, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr("biomni_bridge.adapter.subprocess.run", fake_timeout)

    errors: list[BaseException] = []

    def export_in_worker():
        try:
            adapter.export_pdf()
        except BaseException as exc:  # pragma: no cover - relayed below
            errors.append(exc)

    worker = threading.Thread(target=export_in_worker)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert "timed out after 60 seconds" in str(errors[0])
