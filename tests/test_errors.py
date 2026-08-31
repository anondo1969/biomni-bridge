from biomni_bridge.errors import describe_model_error, safe_exception_text


class FakeHttpError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def test_gateway_error_is_concise_and_drops_html() -> None:
    exc = FakeHttpError(504, "<!DOCTYPE html><html><body>nginx secret-value</body></html>")
    info = describe_model_error(
        exc,
        base_url="https://example.test/api",
        secret="secret-value",
    )

    assert "HTTP 504" in info.message
    assert "upstream model service" in info.message
    assert "nginx" not in info.message
    assert "secret-value" not in info.message
    assert info.status == "Upstream gateway error (HTTP 504)"


def test_authentication_error_has_actionable_message() -> None:
    info = describe_model_error(
        FakeHttpError(401, "bad key"),
        base_url="https://example.test/api",
        secret="secret",
    )
    assert "API key" in info.message
    assert "HTTP 401" in info.message


def test_unknown_exception_is_secret_scrubbed_and_bounded() -> None:
    text = safe_exception_text(RuntimeError("token=abc123 " + "x" * 1000), "abc123")
    assert "abc123" not in text
    assert "***" in text
    assert len(text) < 600


def test_strict_role_400_gets_specific_compatibility_message() -> None:
    class Response:
        status_code = 400

    class BadRequestError(Exception):
        status_code = 400
        response = Response()

    exc = BadRequestError("Conversation roles must alternate user/assistant/user/assistant/...")
    info = describe_model_error(exc, base_url="https://example.test/api")
    assert "role alternation" in info.message
    assert "compatibility shim" in info.message
    assert info.status.endswith("(HTTP 400)")
