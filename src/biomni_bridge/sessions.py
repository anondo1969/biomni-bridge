from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, replace
from threading import RLock

from .adapter import BiomniAdapter
from .config import Settings


@dataclass(slots=True)
class SessionRuntime:
    """Server-memory state for one Gradio browser session."""

    settings: Settings
    adapter: BiomniAdapter
    last_seen: float


class SessionRegistry:
    """Keep non-deepcopyable Biomni adapters isolated by Gradio session hash.

    Gradio session state must be deepcopy-able, while ``BiomniAdapter`` owns
    locks and a lazy A1 instance. Gradio documents the session-hash registry
    pattern for exactly this class of object. Credentials therefore stay in
    server memory and are never returned to the browser as component state.
    """

    def __init__(self, *, ttl_seconds: int = 3600, max_sessions: int = 128):
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._lock = RLock()
        self._sessions: dict[str, SessionRuntime] = {}

    @staticmethod
    def _dispose(runtime: SessionRuntime) -> None:
        try:
            runtime.adapter.reset()
        except Exception:  # noqa: BLE001 - cleanup must remain best effort
            pass

    def _cleanup_stale_locked(self, now: float) -> None:
        stale = [key for key, runtime in self._sessions.items() if now - runtime.last_seen > self._ttl_seconds]
        for key in stale:
            runtime = self._sessions.pop(key)
            self._dispose(runtime)

    def connect(self, session_id: str, settings: Settings, model: str | None = None) -> SessionRuntime:
        if not session_id:
            raise RuntimeError("Gradio session ID is unavailable")
        now = time.monotonic()
        with self._lock:
            self._cleanup_stale_locked(now)
            previous = self._sessions.pop(session_id, None)
            if previous is not None:
                self._dispose(previous)

            while len(self._sessions) >= self._max_sessions:
                oldest_key = min(self._sessions, key=lambda key: self._sessions[key].last_seen)
                self._dispose(self._sessions.pop(oldest_key))

            runtime_settings = settings
            if settings.credential_mode == "ui":
                session_token = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
                session_output = settings.output_dir / "sessions" / session_token
                session_output.mkdir(parents=True, exist_ok=True)
                runtime_settings = replace(settings, output_dir=session_output)

            runtime = SessionRuntime(
                settings=runtime_settings,
                adapter=BiomniAdapter(runtime_settings, model or runtime_settings.default_model),
                last_seen=now,
            )
            self._sessions[session_id] = runtime
            return runtime

    def get(self, session_id: str) -> SessionRuntime | None:
        if not session_id:
            return None
        now = time.monotonic()
        with self._lock:
            self._cleanup_stale_locked(now)
            runtime = self._sessions.get(session_id)
            if runtime is not None:
                runtime.last_seen = now
            return runtime

    def require(self, session_id: str) -> SessionRuntime:
        runtime = self.get(session_id)
        if runtime is None:
            raise RuntimeError("Connect an API endpoint and key before using Biomni")
        return runtime

    def remove(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            runtime = self._sessions.pop(session_id, None)
            if runtime is not None:
                self._dispose(runtime)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
