from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_project_identity_is_biomni_bridge() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'name = "biomni-bridge"' in pyproject
    assert 'version = "0.3.2"' in pyproject
    assert 'biomni-bridge = "biomni_bridge.app:main"' in pyproject
    assert 'biomni-bridge-endpoint-check = "biomni_bridge.endpoint_check:main"' in pyproject
    assert 'biomni-bridge-endpoint-diagnose = "biomni_bridge.endpoint_diagnose:main"' in pyproject


def test_license_and_security_files_are_present() -> None:
    assert (ROOT / "LICENSE").is_file()
    assert (ROOT / "NOTICE").is_file()
    assert (ROOT / "SECURITY.md").is_file()
    assert (ROOT / "RELEASING.md").is_file()
    assert (ROOT / "THIRD_PARTY_NOTICES.md").is_file()
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'license = "Apache-2.0"' in pyproject
    assert 'license-files = ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"]' in pyproject


def test_dockerfile_remains_multi_stage_and_runs_bridge_cli() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert " AS builder" in text
    assert " AS runtime" in text
    assert "COPY --from=builder" in text
    assert 'CMD ["biomni-bridge"]' in text
    assert "COPY pyproject.toml README.md LICENSE NOTICE THIRD_PARTY_NOTICES.md ./" in text
    assert "THIRD_PARTY_NOTICES.md" not in (ROOT / ".dockerignore").read_text().splitlines()


def test_release_workflow_uses_multiplatform_buildx() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()

    assert "actions/checkout@v7" in text
    assert "docker/setup-qemu-action@v4" in text
    assert "docker/setup-buildx-action@v4" in text
    assert "docker/build-push-action@v7" in text

    assert "context: ." in text
    assert "platforms: linux/amd64,linux/arm64" in text
    assert "push: true" in text

    assert "packages: write" in text
    assert "id-token: write" in text
    assert "registry: ghcr.io" in text

    assert "cache-from:" in text
    assert "cache-to:" in text
    assert "type=gha" in text


def test_docker_check_never_pushes_registry_images() -> None:
    text = (ROOT / ".github" / "workflows" / "docker-check.yml").read_text()

    assert "actions/checkout@v7" in text
    assert "docker/setup-buildx-action@v4" in text
    assert "docker/build-push-action@v7" in text
    assert "context: ." in text
    assert "push: false" in text
    assert "push: false" in text
    assert "workflow_dispatch:" in text


def test_ci_actions_are_node24_generation() -> None:
    workflows = "\n".join(path.read_text() for path in (ROOT / ".github" / "workflows").glob("*.yml"))
    assert "actions/checkout@v4" not in workflows
    assert "actions/checkout@v7" in workflows
    assert "actions/setup-python@v5" not in workflows
    assert "actions/setup-python@v7" in workflows
    assert "actions/upload-artifact@v4" not in workflows
    assert "actions/download-artifact@v4" not in workflows


def test_release_workflow_uses_trusted_pypi_publishing_and_tag_guard() -> None:
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert 'tags:' in text and '"v*"' in text
    assert "id-token: write" in text
    assert "environment:" in text
    assert "name: pypi" in text
    assert "pypa/gh-action-pypi-publish@release/v1" in text
    assert "actions/upload-artifact@v7" in text
    assert "actions/download-artifact@v8" in text
    assert "does not match pyproject version" in text
    assert "needs:" in text
    assert "- package" in text
    assert "- docker" in text
    assert "gh release create" in text
    assert "PYPI_TOKEN" not in text


def test_dependabot_does_not_autobump_compatibility_sensitive_python_stack() -> None:
    text = (ROOT / ".github" / "dependabot.yml").read_text()
    assert "package-ecosystem: pip" in text
    assert "open-pull-requests-limit: 0" in text
    assert "applies-to: security-updates" in text
    assert "package-ecosystem: github-actions" in text
    assert "package-ecosystem: docker" in text


def test_makefile_exposes_release_and_endpoint_targets() -> None:
    text = (ROOT / "Makefile").read_text()
    assert "package:" in text
    assert "release-check:" in text
    assert "build-amd64:" in text
    assert "build-arm64:" in text
    assert "build-multi:" in text
    assert "build-multi-push:" in text
    assert "endpoint-diagnose:" in text
    assert "biomni-bridge-endpoint-diagnose" in text


def test_compose_exposes_compatibility_controls_and_fixed_container_paths() -> None:
    text = (ROOT / "docker-compose.yml").read_text()
    assert "ghcr.io/YOUR_USERNAME/biomni-bridge:latest" in text
    assert "BIOMNI_DATA_PATH: /data" in text
    assert "BIOMNI_OUTPUT_PATH: /output" in text
    assert "BIOMNI_DEBUG_LLM_REQUESTS: ${BIOMNI_DEBUG_LLM_REQUESTS:-false}" in text
    assert "BIOMNI_LLM_STREAM_TRANSPORT: ${BIOMNI_LLM_STREAM_TRANSPORT:-true}" in text
    assert "BIOMNI_QWEN_DISABLE_THINKING: ${BIOMNI_QWEN_DISABLE_THINKING:-true}" in text
    assert "BIOMNI_UI_PUBLIC_ENDPOINTS_ONLY: ${BIOMNI_UI_PUBLIC_ENDPOINTS_ONLY:-true}" in text


def test_docker_runtime_defaults_enable_long_generation_compatibility() -> None:
    text = (ROOT / "Dockerfile").read_text()
    assert "BIOMNI_DATA_PATH=/data" in text
    assert "BIOMNI_OUTPUT_PATH=/output" in text
    assert "BIOMNI_LLM_STREAM_TRANSPORT=true" in text
    assert "BIOMNI_QWEN_DISABLE_THINKING=true" in text
    assert "GRADIO_SERVER_NAME=0.0.0.0" in text


def test_model_server_tuning_guide_covers_streaming_and_proxy_limits() -> None:
    text = (ROOT / "MODEL_SERVER_TUNING.md").read_text()
    assert "proxy_read_timeout 1200s" in text
    assert "proxy_buffering off" in text
    assert "client_max_body_size 8m" in text
    assert "enable_thinking" in text


def test_security_doc_states_shared_process_is_not_hostile_tenant_boundary() -> None:
    text = (ROOT / "SECURITY.md").read_text()
    assert "not suitable as an open public multi-tenant service" in text
    assert "default_config.api_key" in text
    assert "separate container/process per user" in text
    assert "SSRF" in text
