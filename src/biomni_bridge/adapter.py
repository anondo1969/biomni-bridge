from __future__ import annotations

import html
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .llm_compat import LLMRequestDiagnostics, StrictRoleCompatLLM

# Biomni 0.0.8 keeps provider settings in a process-global default_config.
# Guard every mutation so future multi-session use cannot cross-contaminate
# endpoint credentials even if more than one adapter instance exists.
_BIOMNI_GLOBAL_CONFIG_LOCK = threading.RLock()

_SOLUTION_RE = re.compile(r"<solution>(.*?)</solution>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"</?(?:solution|execute|observation|think)>", re.IGNORECASE)

# The UI renders execution trace as Markdown. Only Biomni's known pseudo-XML
# blocks are interpreted; everything else is HTML-escaped first so model/tool
# output cannot become active markup. Think blocks are intentionally omitted
# from the UI trace.
_EXECUTE_BLOCK_RE = re.compile(r"<execute>(.*?)</execute>", re.IGNORECASE | re.DOTALL)
_EXECUTE_OPEN_RE = re.compile(r"<execute>(.*)\Z", re.IGNORECASE | re.DOTALL)
_OBSERVATION_BLOCK_RE = re.compile(r"<observation>(.*?)</observation>", re.IGNORECASE | re.DOTALL)
_OBSERVATION_OPEN_RE = re.compile(r"<observation>(.*)\Z", re.IGNORECASE | re.DOTALL)
_SOLUTION_BLOCK_RE = re.compile(r"<solution>(.*?)</solution>", re.IGNORECASE | re.DOTALL)
_SOLUTION_OPEN_RE = re.compile(r"<solution>(.*)\Z", re.IGNORECASE | re.DOTALL)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think>.*\Z", re.IGNORECASE | re.DOTALL)


def _markdown_fence(content: str, language: str = "") -> str:
    """Fence arbitrary content without being broken by embedded backticks."""
    longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
    fence = "`" * max(3, longest + 1)
    suffix = language if language else ""
    return f"{fence}{suffix}\n{content.strip()}\n{fence}"


def format_trace_markdown(text: str) -> str:
    """Safely render Biomni execution pseudo-XML as readable Markdown.

    ``<execute>`` and ``<observation>`` become code/text fences and solutions
    get a label. Unknown markup is escaped, so content such as ``p < 0.05`` is
    displayed literally instead of being interpreted as HTML. Biomni ``think``
    blocks are removed rather than exposing hidden reasoning in the UI.
    """
    if not text:
        return ""

    working = _THINK_BLOCK_RE.sub("", text)
    working = _THINK_OPEN_RE.sub("", working)
    working = re.sub(r"</?think>", "", working, flags=re.IGNORECASE)

    blocks: list[str] = []
    marker = uuid.uuid4().hex

    def stash(rendered: str) -> str:
        token = f"@@BIOMNI_{marker}_{len(blocks)}@@"
        blocks.append(rendered)
        return token

    def execute(match: re.Match[str]) -> str:
        return stash(_markdown_fence(match.group(1), "python"))

    def observation(match: re.Match[str]) -> str:
        return stash(_markdown_fence(match.group(1), "text"))

    def solution(match: re.Match[str]) -> str:
        # Preserve Markdown emphasis/lists while neutralising raw HTML.
        content = html.escape(match.group(1).strip(), quote=False)
        return stash(f"**Solution**\n\n{content}")

    working = _EXECUTE_BLOCK_RE.sub(execute, working)
    working = _OBSERVATION_BLOCK_RE.sub(observation, working)
    working = _SOLUTION_BLOCK_RE.sub(solution, working)

    # Streaming can surface a block before its closing tag arrives. Treat the
    # rest of that individual step as the partial block so Markdown stays valid.
    working = _EXECUTE_OPEN_RE.sub(execute, working)
    working = _OBSERVATION_OPEN_RE.sub(observation, working)
    working = _SOLUTION_OPEN_RE.sub(solution, working)

    rendered = html.escape(working, quote=False)
    for index, block in enumerate(blocks):
        rendered = rendered.replace(f"@@BIOMNI_{marker}_{index}@@", block)
    return rendered.strip()


@dataclass(slots=True)
class AgentUpdate:
    trace: str
    final_answer: str | None = None


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(part for part in parts if part)
    return str(content) if content is not None else ""


def _extract_solution(text: str) -> str | None:
    match = _SOLUTION_RE.search(text or "")
    if not match:
        return None
    solution = re.sub(
        r"<think>.*?</think>",
        "",
        match.group(1),
        flags=re.IGNORECASE | re.DOTALL,
    )
    return solution.strip() or None


def _clean_fallback_answer(text: str) -> str:
    text = re.sub(r"<execute>.*?</execute>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<observation>.*?</observation>", "", text, flags=re.IGNORECASE | re.DOTALL)
    # If the public stream did not expose a <solution>, do not accidentally use
    # a hidden reasoning block as the fallback answer.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<think>.*\Z", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = _TAG_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class BiomniAdapter:
    """Thin compatibility layer around Biomni 0.0.8 public APIs.

    The wrapper deliberately uses ``A1.go_stream()`` rather than copying A1's
    LangGraph workflow. A small compatibility seam remains for extracting the
    final answer and for worker-thread PDF generation because Biomni 0.0.8's
    native PDF helper installs ``SIGALRM`` handlers, which Python only permits
    in the main thread.
    """

    def __init__(self, settings: Settings, model: str | None = None):
        self.settings = settings
        self.settings.prepare_directories()
        self.requested_model = (model or settings.default_model or "").strip()
        self._built_model: str | None = None
        self._lock = threading.RLock()
        self._agent: Any = None
        self._has_completed_run = False

    @property
    def built_model(self) -> str | None:
        return self._built_model

    def _snapshot_global_config(self, default_config: Any) -> dict[str, Any]:
        names = (
            "path",
            "llm",
            "source",
            "base_url",
            "timeout_seconds",
            "temperature",
            "use_tool_retriever",
            "commercial_mode",
        )
        # Deliberately exclude api_key. This wrapper owns the custom endpoint
        # secret lifecycle and never restores a key into the global config.
        return {name: getattr(default_config, name) for name in names}

    @staticmethod
    def _restore_global_config(default_config: Any, snapshot: dict[str, Any]) -> None:
        for name, value in snapshot.items():
            setattr(default_config, name, value)
        default_config.api_key = None

    def _configure_global_nonsecret(self, default_config: Any, model: str) -> None:
        default_config.path = str(self.settings.data_dir)
        default_config.llm = model
        default_config.source = "Custom"
        default_config.base_url = self.settings.base_url
        default_config.timeout_seconds = self.settings.timeout_seconds
        default_config.temperature = self.settings.temperature
        default_config.use_tool_retriever = self.settings.use_tool_retriever
        default_config.commercial_mode = self.settings.commercial_mode
        # Biomni 0.0.8 prints default_config during A1 construction. Keep the
        # real key out of that object whenever the agent is idle or rendering.
        default_config.api_key = None

    def _build_agent(self, model: str) -> Any:
        from biomni.agent import A1
        from biomni.config import default_config

        if not model:
            raise ValueError("A model name is required")

        with _BIOMNI_GLOBAL_CONFIG_LOCK:
            snapshot = self._snapshot_global_config(default_config)
            self._configure_global_nonsecret(default_config, model)
            kwargs: dict[str, Any] = {
                "path": str(self.settings.data_dir),
                "llm": model,
                "source": "Custom",
                "base_url": self.settings.base_url,
                "api_key": self.settings.api_key,
                "timeout_seconds": self.settings.timeout_seconds,
                "use_tool_retriever": self.settings.use_tool_retriever,
                "commercial_mode": self.settings.commercial_mode,
            }
            if self.settings.skip_data_download:
                kwargs["expected_data_lake_files"] = []

            try:
                candidate = A1(**kwargs)
                # Biomni 0.0.8 records <observation> results as AIMessage,
                # producing assistant/assistant turns after tool execution.
                # Keep Biomni's internal state unchanged, but normalize those
                # turns at the provider boundary for strict strict gateways.
                if hasattr(candidate, "llm"):
                    diagnostics = None
                    if self.settings.debug_llm_requests:
                        diagnostics = LLMRequestDiagnostics(
                            self.settings.output_dir / "llm-debug",
                            redactions=(self.settings.api_key,),
                        )
                    candidate.llm = StrictRoleCompatLLM(
                        candidate.llm,
                        diagnostics=diagnostics,
                        stream_transport=self.settings.llm_stream_transport,
                        disable_qwen_thinking=self.settings.qwen_disable_thinking,
                    )
            except Exception:
                # Restore non-secret global state, but always scrub api_key.
                # The in-memory Settings object remains the secret source.
                self._restore_global_config(default_config, snapshot)
                raise

            # Some 0.0.8 database tools read the remaining default_config fields
            # at execution time. Keep those synchronized, but leave the key
            # scrubbed whenever no Biomni graph step is active.
            self._configure_global_nonsecret(default_config, model)
            return candidate

    def _ensure_agent(self) -> Any:
        if not self.requested_model:
            raise ValueError("Select or type a model ID first")
        if self._agent is None or self._built_model != self.requested_model:
            self._agent = self._build_agent(self.requested_model)
            self._built_model = self.requested_model
            self._has_completed_run = False
        return self._agent

    def _next_stream_step(self, iterator: Iterator[dict[str, Any]]) -> dict[str, Any]:
        """Advance Biomni while exposing the key globally only for that call.

        Some Biomni 0.0.8 database helpers create an LLM from ``default_config``
        rather than the A1 instance. They therefore need the key while a graph
        step is actively running. The key is removed before control returns to
        the Gradio/UI layer, including on exceptions and StopIteration.
        """
        from biomni.config import default_config

        with _BIOMNI_GLOBAL_CONFIG_LOCK:
            # ``default_config`` is process-global in Biomni 0.0.8. Another
            # adapter may have been built since this iterator was created, so
            # restore this adapter's endpoint/model settings before every graph
            # step as well as scoping the secret to the active call.
            model = self._built_model or self.requested_model
            self._configure_global_nonsecret(default_config, model)
            default_config.api_key = self.settings.api_key
            try:
                return next(iterator)
            finally:
                default_config.api_key = None

    def set_model(self, model: str) -> str:
        model = (model or "").strip()
        if not model:
            self.requested_model = ""
            return "Type a model ID before running a task."
        self.requested_model = model
        if self._built_model and self._built_model != model:
            return f"{model} will be used for the next task."
        return f"Using {model}."

    def reset(self, model: str | None = None) -> None:
        with self._lock:
            if model is not None:
                self.requested_model = model.strip()
            self._agent = None
            self._built_model = None
            self._has_completed_run = False
            # Best effort: scrub an imported Biomni global key on reset too.
            try:
                from biomni.config import default_config

                with _BIOMNI_GLOBAL_CONFIG_LOCK:
                    default_config.api_key = None
            except ImportError:
                pass

    def stream(self, prompt: str) -> Iterator[AgentUpdate]:
        prompt = (prompt or "").strip()
        if not prompt:
            return

        with self._lock:
            agent = self._ensure_agent()
            last_trace = ""
            final_answer: str | None = None
            final_answer_was_streamed = False
            self._has_completed_run = False

            # go_stream() is the public streaming API in Biomni 0.0.8. It
            # yields pretty-printed execution steps and maintains the state
            # needed for the PDF exporter.
            iterator = iter(agent.go_stream(prompt))
            while True:
                try:
                    step = self._next_stream_step(iterator)
                except StopIteration:
                    break

                trace = str(step.get("output", "")).strip()
                if not trace:
                    continue
                last_trace = trace
                streamed_solution = _extract_solution(trace)
                if streamed_solution:
                    final_answer = streamed_solution
                    final_answer_was_streamed = True
                yield AgentUpdate(trace=trace, final_answer=final_answer)

            # Only emit a synthetic final update when go_stream() did not put a
            # <solution> in the stream and we had to recover it from Biomni's
            # conversation state. Avoid repeating the final UI update otherwise.
            if not final_answer:
                final_answer = self._final_answer_from_state(agent)
            self._has_completed_run = True
            if final_answer and not final_answer_was_streamed:
                yield AgentUpdate(trace=last_trace, final_answer=final_answer)

    def _final_answer_from_state(self, agent: Any) -> str | None:
        """Fallback because Biomni 0.0.8 go_stream() exposes trace, not answer."""
        state = getattr(agent, "_conversation_state", None)
        if not isinstance(state, dict):
            return None
        messages = state.get("messages") or []
        for message in reversed(messages):
            if getattr(message, "type", "") not in {"ai", "assistant"}:
                continue
            text = _content_to_text(getattr(message, "content", ""))
            solution = _extract_solution(text)
            if solution:
                return solution
            cleaned = _clean_fallback_answer(text)
            if cleaned:
                return cleaned
        return None

    @staticmethod
    def _save_pdf_without_sigalrm(agent: Any, destination: Path) -> None:
        """Generate a PDF from a Gradio worker without main-thread signals.

        ``A1.save_conversation_history`` installs a SIGALRM timeout. Gradio
        callbacks run in worker threads, where ``signal.signal`` is invalid.
        Generate Biomni's markdown in-process, then run Biomni's own converter
        in a short-lived child process. ``subprocess.run(timeout=60)`` restores
        the timeout guarantee without relying on thread-invalid signal APIs.
        """
        generate_markdown = getattr(agent, "_generate_markdown_content", None)
        if not callable(generate_markdown):
            raise RuntimeError("Biomni 0.0.8 PDF compatibility method is unavailable")

        markdown_content = generate_markdown(include_images=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as temp_file:
                temp_file.write(markdown_content)
                temp_path = Path(temp_file.name)

            converter = (
                "from biomni.utils import convert_markdown_to_pdf; "
                "import sys; convert_markdown_to_pdf(sys.argv[1], sys.argv[2])"
            )
            subprocess.run(
                [sys.executable, "-c", converter, str(temp_path), str(destination)],
                check=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("PDF generation timed out after 60 seconds") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Biomni PDF converter exited with status {exc.returncode}") from exc
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def export_pdf(self) -> Path:
        """Export the most recently completed Biomni run as a PDF."""
        with self._lock:
            if self._agent is None or not self._has_completed_run:
                raise ValueError("Run a task before exporting a PDF")

            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
            destination = self.settings.output_dir / f"biomni-run-{stamp}.pdf"

            if threading.current_thread() is threading.main_thread():
                self._agent.save_conversation_history(str(destination), save_pdf=True)
            else:
                self._save_pdf_without_sigalrm(self._agent, destination)

            if not destination.exists() or destination.stat().st_size == 0:
                raise RuntimeError("Biomni did not create the requested PDF")
            return destination
