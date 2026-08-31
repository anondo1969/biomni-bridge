from argparse import Namespace

from biomni_bridge import endpoint_diagnose
from biomni_bridge.config import Settings


def _args() -> Namespace:
    return Namespace(
        model="Qwen3-235B-A22B",
        timeout=300.0,
        cap=32,
        throughput_tokens=512,
        proxy_idle_timeout=60.0,
    )


def test_diagnostic_flags_slow_non_streaming_generation_without_failing(monkeypatch, capsys):
    settings = Settings(
        base_url="https://example.test/api",
        api_key="secret",
        default_model="Qwen3-235B-A22B",
    )
    monkeypatch.setattr(endpoint_diagnose, "_parse_args", _args)
    monkeypatch.setattr(endpoint_diagnose.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(endpoint_diagnose, "scrub_secret_environment", lambda: None)

    calls = []

    def fake_post(settings_, payload, *, timeout):
        calls.append(payload)
        if len(calls) <= 2:
            return {"usage": {"completion_tokens": 32}}, 3.4, ""
        if len(calls) == 3:
            return {"usage": {"completion_tokens": 2}}, 0.4, ""
        return {"usage": {"completion_tokens": 512}}, 51.0, ""

    monkeypatch.setattr(endpoint_diagnose, "_post", fake_post)

    assert endpoint_diagnose.main() == 0
    output = capsys.readouterr().out
    assert "close to the proxy idle timeout" in output
    assert "1024 tokens:" in output
    assert "exceeds proxy idle timeout" in output
    assert calls[1]["max_completion_tokens"] == 32
    assert calls[2]["chat_template_kwargs"] == {"enable_thinking": False}
    assert calls[3]["chat_template_kwargs"] == {"enable_thinking": False}


def test_diagnostic_returns_nonzero_when_a_probe_cannot_reach_provider(monkeypatch):
    settings = Settings(base_url="https://example.test/api", api_key="secret", default_model="qwen3")
    monkeypatch.setattr(endpoint_diagnose, "_parse_args", _args)
    monkeypatch.setattr(endpoint_diagnose.Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(endpoint_diagnose, "scrub_secret_environment", lambda: None)

    responses = iter(
        [
            (None, 1.0, "HTTP 504"),
            ({"usage": {"completion_tokens": 32}}, 1.0, ""),
            ({"usage": {"completion_tokens": 2}}, 1.0, ""),
            ({"usage": {"completion_tokens": 512}}, 10.0, ""),
        ]
    )
    monkeypatch.setattr(endpoint_diagnose, "_post", lambda *args, **kwargs: next(responses))

    assert endpoint_diagnose.main() == 1
