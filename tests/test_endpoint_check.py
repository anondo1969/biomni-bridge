from biomni_bridge.endpoint_check import _model_ids


def test_endpoint_check_filters_to_unique_chat_models() -> None:
    payload = {
        "data": [
            {"id": "Qwen3-235B-A22B"},
            {"id": "text-embedding-large"},
            {"id": "Qwen3-235B-A22B"},
            "bridge-chat",
        ]
    }
    assert _model_ids(payload) == ["Qwen3-235B-A22B", "bridge-chat"]


def test_endpoint_check_accepts_list_payload() -> None:
    assert _model_ids([{"id": "bridge-a"}, {"id": "clip-vision"}, "bridge-b"]) == ["bridge-a", "bridge-b"]
