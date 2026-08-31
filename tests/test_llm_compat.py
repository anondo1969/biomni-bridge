import sys
import types

from biomni_bridge.llm_compat import StrictRoleCompatLLM, normalize_biomni_messages


class _Message:
    def __init__(self, content):
        self.content = content


class _AIMessage(_Message):
    pass


class _HumanMessage(_Message):
    pass


class _SystemMessage(_Message):
    pass


class _BaseMessageChunk(_Message):
    pass


class _AIMessageChunk(_BaseMessageChunk):
    def __add__(self, other):
        return _AIMessageChunk(self.content + other.content)


def _message_chunk_to_message(chunk):
    return _AIMessage(chunk.content)


def _install_fake_langchain(monkeypatch):
    core = types.ModuleType("langchain_core")
    messages = types.ModuleType("langchain_core.messages")
    messages.AIMessage = _AIMessage
    messages.HumanMessage = _HumanMessage
    messages.SystemMessage = _SystemMessage
    messages.BaseMessageChunk = _BaseMessageChunk
    messages.message_chunk_to_message = _message_chunk_to_message
    monkeypatch.setitem(sys.modules, "langchain_core", core)
    monkeypatch.setitem(sys.modules, "langchain_core.messages", messages)


def test_observation_after_ai_is_sent_as_human_without_mutating_state(monkeypatch):
    _install_fake_langchain(monkeypatch)
    original = [
        _SystemMessage("system"),
        _HumanMessage("task"),
        _AIMessage("<execute>print('x')</execute>"),
        _AIMessage("<observation>x</observation>"),
    ]

    normalized = normalize_biomni_messages(original)

    assert original[-1].__class__ is _AIMessage
    assert normalized[:-1] == original[:-1]
    assert isinstance(normalized[-1], _HumanMessage)
    assert normalized[-1].content == "<observation>x</observation>"


def test_normal_ai_messages_are_not_rewritten(monkeypatch):
    _install_fake_langchain(monkeypatch)
    original = [_HumanMessage("task"), _AIMessage("answer"), _AIMessage("not an observation")]
    normalized = normalize_biomni_messages(original)
    assert normalized == original


def test_observation_without_preceding_ai_is_not_rewritten(monkeypatch):
    _install_fake_langchain(monkeypatch)
    observation = _AIMessage("<observation>x</observation>")
    normalized = normalize_biomni_messages([_HumanMessage("task"), observation])
    assert normalized[-1] is observation


def test_proxy_normalizes_only_provider_input(monkeypatch):
    _install_fake_langchain(monkeypatch)

    class Underlying:
        model_name = "bridge-model"

        def __init__(self):
            self.seen = None

        def invoke(self, input_, *args, **kwargs):
            self.seen = input_
            return "ok"

    underlying = Underlying()
    proxy = StrictRoleCompatLLM(underlying)
    original = [
        _HumanMessage("task"),
        _AIMessage("<execute>work()</execute>"),
        _AIMessage("<observation>done</observation>"),
    ]

    assert proxy.invoke(original) == "ok"
    assert proxy.model_name == "bridge-model"
    assert isinstance(underlying.seen[-1], _HumanMessage)
    assert isinstance(original[-1], _AIMessage)


def test_request_diagnostics_writes_replayable_payload_and_metadata(monkeypatch, tmp_path):
    import json

    from biomni_bridge.llm_compat import LLMRequestDiagnostics

    _install_fake_langchain(monkeypatch)

    class Underlying:
        model_name = "Qwen3-235B-A22B"
        temperature = 0.7
        max_tokens = 8192
        streaming = False

        def _get_request_payload(self, messages, **kwargs):
            return {
                "model": self.model_name,
                "messages": [
                    {"role": "user", "content": messages[0].content},
                    {"role": "assistant", "content": messages[1].content},
                ],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "stream": False,
                "api_key": "top-secret",
                "extra_headers": {"Authorization": "Bearer top-secret"},
            }

        def invoke(self, input_, *args, **kwargs):
            return "ok"

    diagnostics = LLMRequestDiagnostics(tmp_path, redactions=("top-secret",))
    proxy = StrictRoleCompatLLM(Underlying(), diagnostics=diagnostics)

    result = proxy.invoke([_HumanMessage("real Biomni prompt"), _AIMessage("reply")])

    assert result == "ok"
    payload = json.loads((tmp_path / "latest-request.json").read_text())
    metadata = json.loads((tmp_path / "latest-request.meta.json").read_text())
    assert payload["model"] == "Qwen3-235B-A22B"
    assert payload["max_tokens"] == 8192
    assert payload["messages"][0]["content"] == "real Biomni prompt"
    assert "api_key" not in payload
    assert "Authorization" not in payload["extra_headers"]
    assert "top-secret" not in (tmp_path / "latest-request.json").read_text()
    assert metadata["roles"] == ["user", "assistant"]
    assert metadata["message_count"] == 2
    assert metadata["prompt_content_chars"] == len("real Biomni prompt") + len("reply")
    assert metadata["serialized_payload_bytes"] > 0
    assert metadata["result"] == "success"
    assert metadata["elapsed_seconds"] >= 0


def test_request_diagnostics_records_http_status_without_error_body(monkeypatch, tmp_path):
    import json

    from biomni_bridge.llm_compat import LLMRequestDiagnostics

    _install_fake_langchain(monkeypatch)

    class GatewayError(RuntimeError):
        status_code = 504

    class Underlying:
        model_name = "bridge-model"

        def invoke(self, input_, *args, **kwargs):
            raise GatewayError("nginx body that should not be persisted")

    diagnostics = LLMRequestDiagnostics(tmp_path, redactions=("secret",))
    proxy = StrictRoleCompatLLM(Underlying(), diagnostics=diagnostics)

    try:
        proxy.invoke([_HumanMessage("task")])
    except GatewayError:
        pass
    else:
        raise AssertionError("GatewayError should propagate")

    metadata = json.loads((tmp_path / "latest-request.meta.json").read_text())
    assert metadata["result"] == "error"
    assert metadata["http_status"] == 504
    assert metadata["error_type"] == "GatewayError"
    assert "nginx body" not in (tmp_path / "latest-request.meta.json").read_text()


def test_qwen3_requests_disable_thinking_via_extra_body(monkeypatch):
    _install_fake_langchain(monkeypatch)

    class Underlying:
        model_name = "Qwen3-235B-A22B"

        def __init__(self):
            self.kwargs = None

        def invoke(self, input_, *args, **kwargs):
            self.kwargs = kwargs
            return "ok"

    underlying = Underlying()
    proxy = StrictRoleCompatLLM(underlying, stream_transport=False, disable_qwen_thinking=True)

    assert proxy.invoke([_HumanMessage("task")]) == "ok"
    assert underlying.kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


def test_non_qwen_request_does_not_add_thinking_option(monkeypatch):
    _install_fake_langchain(monkeypatch)

    class Underlying:
        model_name = "gemma3-27b"

        def __init__(self):
            self.kwargs = None

        def invoke(self, input_, *args, **kwargs):
            self.kwargs = kwargs
            return "ok"

    underlying = Underlying()
    proxy = StrictRoleCompatLLM(underlying, stream_transport=False, disable_qwen_thinking=True)
    assert proxy.invoke([_HumanMessage("task")]) == "ok"
    assert "extra_body" not in underlying.kwargs


def test_stream_transport_aggregates_chunks_back_to_message(monkeypatch):
    _install_fake_langchain(monkeypatch)

    class Underlying:
        model_name = "qwen3"

        def __init__(self):
            self.seen = None
            self.kwargs = None
            self.invoke_called = False

        def stream(self, input_, *args, **kwargs):
            self.seen = input_
            self.kwargs = kwargs
            yield _AIMessageChunk("hello ")
            yield _AIMessageChunk("world")

        def invoke(self, input_, *args, **kwargs):
            self.invoke_called = True
            return _AIMessage("wrong path")

    underlying = Underlying()
    proxy = StrictRoleCompatLLM(underlying, stream_transport=True, disable_qwen_thinking=True)
    result = proxy.invoke([_HumanMessage("task")])

    assert isinstance(result, _AIMessage)
    assert result.content == "hello world"
    assert underlying.invoke_called is False
    assert underlying.kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_diagnostics_flatten_extra_body_and_record_stream_transport(monkeypatch, tmp_path):
    import json

    from biomni_bridge.llm_compat import LLMRequestDiagnostics

    _install_fake_langchain(monkeypatch)

    class Underlying:
        model_name = "Qwen3-235B-A22B"
        extra_body = {"some_provider_flag": True}

        def _get_request_payload(self, messages, **kwargs):
            return {
                "model": self.model_name,
                "messages": [{"role": "user", "content": messages[0].content}],
                "stream": kwargs.get("stream", False),
                "extra_body": kwargs.get("extra_body"),
            }

        def stream(self, input_, *args, **kwargs):
            yield _AIMessageChunk("OK")

    diagnostics = LLMRequestDiagnostics(tmp_path)
    proxy = StrictRoleCompatLLM(Underlying(), diagnostics=diagnostics, stream_transport=True)
    assert proxy.invoke([_HumanMessage("task")]).content == "OK"

    payload = json.loads((tmp_path / "latest-request.json").read_text())
    assert payload["stream"] is True
    assert "extra_body" not in payload
    assert payload["some_provider_flag"] is True
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_qwen_thinking_merge_preserves_existing_provider_options(monkeypatch):
    _install_fake_langchain(monkeypatch)

    class Underlying:
        model_name = "Qwen/Qwen3.8-27B"
        extra_body = {
            "some_provider_flag": True,
            "chat_template_kwargs": {"foo": "configured", "enable_thinking": True},
        }

        def __init__(self):
            self.kwargs = None

        def invoke(self, input_, *args, **kwargs):
            self.kwargs = kwargs
            return "ok"

    underlying = Underlying()
    proxy = StrictRoleCompatLLM(underlying, stream_transport=False)
    proxy.invoke(
        [_HumanMessage("task")],
        extra_body={"other_provider_flag": 7, "chat_template_kwargs": {"bar": "call"}},
    )

    extra_body = underlying.kwargs["extra_body"]
    assert extra_body["some_provider_flag"] is True
    assert extra_body["other_provider_flag"] == 7
    assert extra_body["chat_template_kwargs"] == {
        "foo": "configured",
        "bar": "call",
        "enable_thinking": False,
    }


def test_qwen_thinking_control_can_be_disabled(monkeypatch):
    _install_fake_langchain(monkeypatch)

    class Underlying:
        model_name = "qwen3"

        def __init__(self):
            self.kwargs = None

        def invoke(self, input_, *args, **kwargs):
            self.kwargs = kwargs
            return "ok"

    underlying = Underlying()
    proxy = StrictRoleCompatLLM(underlying, stream_transport=False, disable_qwen_thinking=False)
    assert proxy.invoke([_HumanMessage("task")]) == "ok"
    assert "extra_body" not in underlying.kwargs
