from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}
CREDENTIAL_MODES = {"auto", "env", "ui"}


_SECRET_ENV_NAMES = (
    "BIOMNI_CUSTOM_API_KEY",
    "BIOMNI_API_KEY",
)


def scrub_secret_environment() -> None:
    """Remove wrapper API-key variables from this process environment.

    Docker may still receive the value at process start in env/auto mode, but
    Biomni-generated subprocesses and Python snippets should not inherit an
    easy-to-read copy. In UI credential mode the key never needs to enter the
    process environment at all.
    """
    for name in _SECRET_ENV_NAMES:
        os.environ.pop(name, None)


class ConfigError(RuntimeError):
    """Raised when runtime configuration is missing or invalid."""


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _bool_env(name: str, default: bool) -> bool:
    value = _first_env(name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise ConfigError(f"{name} must be one of true/false, 1/0, yes/no, or on/off")


def _int_env(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    value = _first_env(name)
    if value is None:
        result = default
    else:
        try:
            result = int(value)
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer, got {value!r}") from exc
    if minimum is not None and result < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ConfigError(f"{name} must be at most {maximum}")
    return result


def _float_env(name: str, default: float, *, minimum: float | None = None, maximum: float | None = None) -> float:
    value = _first_env(name)
    if value is None:
        result = default
    else:
        try:
            result = float(value)
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number, got {value!r}") from exc
    if minimum is not None and result < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ConfigError(f"{name} must be at most {maximum}")
    return result


def _list_env(name: str) -> tuple[str, ...]:
    raw = _first_env(name)
    if not raw:
        return ()
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def _credential_mode_env() -> str:
    mode = (_first_env("BIOMNI_CREDENTIAL_MODE") or "auto").lower()
    if mode not in CREDENTIAL_MODES:
        allowed = ", ".join(sorted(CREDENTIAL_MODES))
        raise ConfigError(f"BIOMNI_CREDENTIAL_MODE must be one of: {allowed}")
    return mode


def _validate_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("The base API URL must be an absolute http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("The base API URL must not contain embedded username/password credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError("The base API URL must not contain a query string or fragment")
    lowered_path = parsed.path.rstrip("/").lower()
    if lowered_path.endswith("/chat/completions") or lowered_path.endswith("/models"):
        raise ConfigError(
            "Pass the API prefix only, not /chat/completions or /models; "
            "for example https://models.example.org/v1"
        )
    return value


def _validate_public_ui_endpoint(value: str, allowed_hosts: tuple[str, ...]) -> None:
    """Reject UI-provided endpoints that could target a private cluster network.

    A Gradio server makes UI-requested endpoint calls from its own network position. Allowing an
    arbitrary URL would therefore be an SSRF primitive. UI mode accepts HTTPS
    endpoints resolving only to globally routable addresses. If an allowlist is
    configured, the hostname must also appear in it.
    """
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        raise ConfigError("UI-provided API URLs must use https://")

    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        raise ConfigError("The base API URL must include a hostname")

    normalized_allowed = {host.rstrip(".").lower() for host in allowed_hosts}
    if normalized_allowed and hostname not in normalized_allowed:
        raise ConfigError(f"API hostname {hostname!r} is not in BIOMNI_ALLOWED_API_HOSTS")

    if normalized_allowed:
        # A deployment-maintained exact hostname allowlist is a stronger policy
        # than a one-time DNS classification and avoids false negatives when
        # build/test environments do not have external DNS access.
        return

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        if not literal.is_global:
            raise ConfigError("UI-provided API URLs must not target private, loopback, or reserved IP addresses")
        return

    try:
        answers = socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ConfigError(f"Could not resolve API hostname {hostname!r}") from exc

    addresses = {item[4][0] for item in answers if item and item[4]}
    if not addresses:
        raise ConfigError(f"Could not resolve API hostname {hostname!r}")
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ConfigError(f"Unexpected DNS result for API hostname {hostname!r}") from exc
        if not resolved.is_global:
            raise ConfigError(
                f"API hostname {hostname!r} resolves to a non-public address; "
                "hosted UI credentials may only target public endpoints"
            )


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings for Biomni Bridge.

    The application can start without an API key. In ``ui`` credential
    mode, each browser session supplies its own endpoint/key and gets an
    independent lazy Biomni adapter. The API key is excluded from the dataclass
    representation so accidental ``repr(settings)`` logging cannot expose it.
    """

    base_url: str = ""
    api_key: str = field(default="", repr=False)
    # Native/local execution defaults to directories below the current
    # working directory. Docker overrides these with BIOMNI_DATA_PATH=/data
    # and BIOMNI_OUTPUT_PATH=/output.
    data_dir: Path = field(default_factory=lambda: Path.cwd() / "data")
    output_dir: Path = field(default_factory=lambda: Path.cwd() / "output")
    default_model: str = ""
    fallback_models: tuple[str, ...] = ()
    timeout_seconds: int = 1200
    temperature: float = 0.7
    use_tool_retriever: bool = True
    commercial_mode: bool = False
    discover_models: bool = True
    skip_data_download: bool = True
    server_name: str = "127.0.0.1"
    server_port: int = 7860
    debug_llm_requests: bool = False
    llm_stream_transport: bool = True
    qwen_disable_thinking: bool = True
    credential_mode: str = "auto"
    ui_public_endpoints_only: bool = True
    allowed_api_hosts: tuple[str, ...] = ()
    session_ttl_seconds: int = 3600

    @classmethod
    def from_env(cls) -> Settings:
        # BIOMNI_CUSTOM_* are the canonical names because they match Biomni
        # 0.0.8. The shorter aliases are accepted for convenience. Deliberately
        # do not consume OPENAI_BASE_URL/OPENAI_API_KEY: unrelated shell config
        # should never silently redirect this application.
        mode = _credential_mode_env()
        raw_base_url = _first_env("BIOMNI_CUSTOM_BASE_URL", "BIOMNI_BASE_URL") or ""
        raw_api_key = _first_env("BIOMNI_CUSTOM_API_KEY", "BIOMNI_API_KEY") or ""

        base_url = _validate_base_url(raw_base_url) if raw_base_url else ""
        api_key = "" if mode == "ui" else raw_api_key

        if mode == "env" and (not base_url or not api_key):
            raise ConfigError(
                "BIOMNI_CREDENTIAL_MODE=env requires BIOMNI_CUSTOM_BASE_URL and BIOMNI_CUSTOM_API_KEY"
            )
        if api_key and not base_url:
            raise ConfigError("An API key was provided without BIOMNI_CUSTOM_BASE_URL")

        return cls(
            base_url=base_url,
            api_key=api_key,
            data_dir=Path(
                _first_env("BIOMNI_DATA_PATH", "BIOMNI_PATH") or str(Path.cwd() / "data")
            ).expanduser(),
            output_dir=Path(
                _first_env("BIOMNI_OUTPUT_PATH", "BIOMNI_EXPORT_DIR") or str(Path.cwd() / "output")
            ).expanduser(),
            default_model=_first_env("BIOMNI_MODEL", "BIOMNI_LLM", "BIOMNI_LLM_MODEL") or "",
            fallback_models=_list_env("BIOMNI_MODELS"),
            timeout_seconds=_int_env("BIOMNI_TIMEOUT_SECONDS", 1200, minimum=1),
            temperature=_float_env("BIOMNI_TEMPERATURE", 0.7, minimum=0.0, maximum=2.0),
            use_tool_retriever=_bool_env("BIOMNI_USE_TOOL_RETRIEVER", True),
            commercial_mode=_bool_env("BIOMNI_COMMERCIAL_MODE", False),
            discover_models=_bool_env("BIOMNI_DISCOVER_MODELS", True),
            skip_data_download=_bool_env("BIOMNI_SKIP_DATA_DOWNLOAD", True),
            server_name=_first_env("GRADIO_SERVER_NAME") or "127.0.0.1",
            server_port=_int_env("GRADIO_SERVER_PORT", 7860, minimum=1, maximum=65535),
            debug_llm_requests=_bool_env("BIOMNI_DEBUG_LLM_REQUESTS", False),
            llm_stream_transport=_bool_env("BIOMNI_LLM_STREAM_TRANSPORT", True),
            qwen_disable_thinking=_bool_env("BIOMNI_QWEN_DISABLE_THINKING", True),
            credential_mode=mode,
            ui_public_endpoints_only=_bool_env("BIOMNI_UI_PUBLIC_ENDPOINTS_ONLY", True),
            allowed_api_hosts=_list_env("BIOMNI_ALLOWED_API_HOSTS"),
            session_ttl_seconds=_int_env("BIOMNI_SESSION_TTL_SECONDS", 3600, minimum=300),
        )

    @property
    def has_credentials(self) -> bool:
        return bool(self.base_url and self.api_key)

    def require_credentials(self) -> None:
        if not self.has_credentials:
            raise ConfigError(
                "This command requires BIOMNI_CUSTOM_BASE_URL and BIOMNI_CUSTOM_API_KEY in the environment"
            )

    @property
    def server_credentials_enabled(self) -> bool:
        return self.credential_mode in {"auto", "env"} and self.has_credentials

    @property
    def ui_credentials_enabled(self) -> bool:
        return self.credential_mode in {"auto", "ui"}

    def with_ui_credentials(self, base_url: str, api_key: str) -> Settings:
        """Return per-session settings from credentials entered in the UI."""
        if not self.ui_credentials_enabled:
            raise ConfigError("This deployment does not accept credentials from the UI")

        validated_url = _validate_base_url(base_url)
        clean_key = (api_key or "").strip()
        if not clean_key:
            raise ConfigError("Enter an API key")
        if self.ui_public_endpoints_only:
            _validate_public_ui_endpoint(validated_url, self.allowed_api_hosts)

        return replace(self, base_url=validated_url, api_key=clean_key, credential_mode="ui")

    def prepare_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
