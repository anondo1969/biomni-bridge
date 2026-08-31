# Biomni Bridge maintainer guide

This repository is intentionally a **thin wrapper** around `biomni==0.0.8`, not a fork of Biomni.

The main maintenance rule is simple: keep the bridge-specific compatibility surface as small as possible, prefer Biomni's public APIs, and delete local workarounds when upstream makes them unnecessary.

## Project identity

- Distribution: `biomni-bridge`
- Python package: `biomni_bridge`
- Main CLI: `biomni-bridge`
- Endpoint check CLI: `biomni-bridge-endpoint-check`
- Endpoint diagnostic CLI: `biomni-bridge-endpoint-diagnose`
- Application title: **Biomni Bridge**
- License: Apache-2.0

The project is independent and is not affiliated with, sponsored by, or endorsed by the Zitnik Lab or by the upstream Biomni project or its authors.

## Architecture

```text
Gradio UI
   │
   ├── SessionRegistry
   │      └── SessionRuntime
   │             ├── per-session Settings
   │             └── lazy BiomniAdapter
   │
   └── BiomniAdapter
          ├── Biomni A1 (public go_stream API)
          ├── process-global default_config guard
          ├── strict-role provider wrapper
          ├── streamed-provider transport
          └── PDF compatibility path
```

The package does not copy or patch the upstream `biomni/` source tree.

## Why the global configuration guard exists

Biomni 0.0.8 supports direct model parameters on `A1`, but some database/retrieval helpers create LLM clients from `biomni.config.default_config` instead. Upstream documentation explicitly notes that direct `A1()` parameters affect the main agent while database queries use the global defaults.

For each active graph step, `BiomniAdapter` therefore:

1. acquires one module-level process lock;
2. applies the active adapter's non-secret model configuration to `default_config`;
3. inserts that adapter's API key immediately before advancing `go_stream()`;
4. keeps the lock held while the graph/tool step executes;
5. clears `default_config.api_key` in `finally`;
6. releases the lock before yielding control back to Gradio.

Do not weaken this ordering without understanding Biomni's database/helper clients.

### Release-blocking cross-session test

`tests/test_session_global_config_isolation.py` reproduces the critical two-user race:

- session A has endpoint/key A;
- session B has endpoint/key B;
- B connects without changing Biomni globals;
- A enters a simulated graph/tool step and sees only A's configuration;
- B disconnects while A is inside the step and is proven to block on the global lock;
- A still sees A's endpoint/key;
- after A finishes, B may clear the global key;
- A's next step restores A's endpoint/key again.

Run it directly with:

```bash
pytest -q tests/test_session_global_config_isolation.py
```

A failure in this test is release-blocking.

This mechanism protects normal queued session configuration. It is not strong isolation against malicious generated code running in the same Python/container process.

## Session model

`SessionRegistry` keys runtimes by Gradio's `request.session_hash`.

Each runtime owns:

- endpoint;
- API key;
- model selection;
- lazy `BiomniAdapter`;
- output subdirectory.

Credentials entered in the UI are held in server memory rather than browser storage. The password textbox is cleared after connection. Disconnect/unload and TTL cleanup release runtime references.

Avoid moving API keys into `gr.State`, `BrowserState`, localStorage, logs, filenames, or diagnostic metadata.

## Endpoint validation and SSRF boundary

UI-provided endpoints default to:

```text
BIOMNI_UI_PUBLIC_ENDPOINTS_ONLY=true
```

They must use HTTPS and must not resolve to private, loopback, link-local, or reserved IP addresses.

An optional exact hostname allowlist is available through:

```text
BIOMNI_ALLOWED_API_HOSTS
```

Environment-configured endpoints may intentionally be private/local for trusted self-hosted use.

Model discovery rejects HTTP redirects so a validated public URL cannot silently redirect the backend toward another target.

## Provider-facing message compatibility

Biomni 0.0.8 can store `<observation>` tool results as an `AIMessage`. Strict OpenAI-compatible gateways may reject the resulting adjacent assistant turns.

`StrictRoleCompatLLM` normalizes only the provider-facing copy of the messages. Biomni's internal graph state is left unchanged.

Do not rewrite the internal conversation state to solve a provider compatibility problem.

## Streamed provider transport

`BIOMNI_LLM_STREAM_TRANSPORT=true` is the default.

The wrapper calls the underlying LangChain model through `.stream()`, aggregates the returned chunks using LangChain message-chunk semantics, and returns a normal complete message to Biomni.

This is intentionally below Biomni's public `A1.go_stream()` layer. Biomni should not need to know whether the remote HTTP response was streamed.

## Qwen3 request-level thinking control

For model IDs containing `qwen3`, the default:

```text
BIOMNI_QWEN_DISABLE_THINKING=true
```

adds provider request data equivalent to:

```json
{
  "chat_template_kwargs": {
    "enable_thinking": false
  }
}
```

Keep this request-scoped. Do not require operators to change a shared model server globally.

## Request diagnostics

`BIOMNI_DEBUG_LLM_REQUESTS=true` captures a sanitized provider-facing request and metadata under the configured output directory.

The diagnostic must never record:

- `Authorization` headers;
- API-key fields;
- the configured secret when it appears in strings;
- raw HTML error pages.

The request body still contains the real prompt and user task. Treat captures as private data and keep diagnostics disabled by default.

## Biomni construction

Every A1 construction must continue to pass:

```python
source="Custom"
base_url=settings.base_url
api_key=settings.api_key
```

Do not rely on model-name provider inference. Custom endpoints can expose model IDs that resemble names from public providers.

When `BIOMNI_SKIP_DATA_DOWNLOAD=true`, the adapter adds:

```python
expected_data_lake_files=[]
```

to avoid an unexpected large download during application startup/task execution.

## PDF export seam

Use Biomni's public `save_conversation_history()` when running on Python's main thread.

Biomni 0.0.8's PDF path installs a `SIGALRM` handler, which Python rejects in Gradio worker threads. The bridge therefore has one deliberate private seam for worker-thread export:

1. obtain Biomni's markdown through `_generate_markdown_content()`;
2. call Biomni's markdown-to-PDF converter in a short-lived child process;
3. enforce an explicit timeout.

Re-check this first on every Biomni upgrade. Remove the workaround as soon as upstream PDF export becomes safe from worker threads.

## Dependency strategy

`requirements.txt` is the canonical exact runtime pin list. `pyproject.toml` reads dependencies from that file.

The compatibility-sensitive core intentionally stays close to the Biomni 0.0.8 environment:

- Python 3.11
- Biomni 0.0.8
- Gradio 5.39.0
- LangChain/LangGraph 0.3-era packages
- OpenAI Python 1.x

Do not independently jump individual components across major generations without a full compatibility review.

The image is not the full Biomni E1 environment. Add specialist scientific packages, R packages, and command-line tools only when a validated workflow requires them.

The historical ToolUniverse/FastMCP stack is intentionally omitted from the default image. If MCP support becomes a requirement, upgrade and test Biomni/ToolUniverse/MCP together rather than restoring an old dependency set blindly.

## Docker design

The Dockerfile is multi-stage.

### Builder

- Python 3.11 slim Bookworm;
- compiler toolchain only in the builder;
- pinned dependencies installed into `/opt/venv`;
- wrapper installed with `--no-deps` after dependency caching.

### Runtime

- Python 3.11 slim Bookworm;
- only the prepared venv copied from builder;
- WeasyPrint/Pango runtime libraries installed;
- non-root UID/GID 1000 by default;
- `/workspace` for generated working files;
- `/data` and `/output` writable;
- `tini` as PID 1;
- Gradio on port 7860;
- runtime smoke test executed during image build.

Direct Python execution binds Gradio to `127.0.0.1` by default. The Dockerfile explicitly sets `GRADIO_SERVER_NAME=0.0.0.0` inside the container; documented Docker commands publish it only to host `127.0.0.1:7860`.

## Multi-platform release requirement

Release images must contain both:

```text
linux/amd64
linux/arm64
```

The release workflow uses Docker's official `docker/github-builder` reusable workflow. AMD64 and ARM64 builds run on native GitHub-hosted runners and Docker assembles one OCI manifest, avoiding slow QEMU emulation for this large scientific image. The builder also supplies signed provenance; the workflow enables an SBOM attestation.

## Tests

Use Python 3.11:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
make check
```

`make check` runs:

- Ruff;
- unit tests;
- runtime compatibility/PDF smoke test.

The smoke test verifies the pinned compatibility surface and imports the scientific packages expected by common generated workflows.

## Release identity

`tests/test_release_config.py` verifies the current distribution/package/CLI names and the required release/security files. Keep repository-wide naming consistent when changing project identity.

## Release automation and PyPI publication

The repository has three workflows:

- `ci.yml`: lint, tests, smoke checks, wheel/sdist build and `twine check`; publishes nothing.
- `docker-check.yml`: Docker validation only; never pushes a registry image.
- `release.yml`: the only publication workflow. A matching `vX.Y.Z` tag runs CI, builds the Python distributions, publishes the native AMD64/ARM64 GHCR image, publishes the same version to PyPI via Trusted Publishing/OIDC, then creates the GitHub Release.

The PyPI publisher identity must match the GitHub owner, repository `biomni-bridge`, workflow filename `release.yml`, and environment `pypi`. Do not add a long-lived `PYPI_TOKEN`; only the final PyPI job receives `id-token: write`.

Routine pip Dependabot version PRs are deliberately disabled because the Biomni 0.0.8 dependency set is compatibility-sensitive and single-package bumps frequently cannot resolve. Dependabot security updates remain enabled; GitHub Actions and Docker version updates remain enabled.

## Upgrading Biomni

For a later Biomni version:

1. change the Biomni pin in `requirements.txt`;
2. review upstream recommended Python/LangChain/LangGraph/Gradio versions as a set;
3. inspect `A1.__init__`, `biomni.llm.get_llm`, and `biomni.config.default_config`;
4. confirm `source="Custom"`, `base_url`, and `api_key` behavior;
5. check whether database helpers still depend on process-global configuration;
6. review `go_stream()` output and final-answer extraction;
7. re-test `expected_data_lake_files=[]`;
8. re-test PDF export from a Gradio worker thread;
9. run the cross-session isolation regression;
10. run a real task against at least one strict OpenAI-compatible endpoint;
11. build and smoke-test both Docker architectures.

Do not copy a newer upstream `biomni/` directory into this repository.

## Release checklist

Before tagging:

```bash
python -m pip install -e '.[dev,release]'
make release-check
```

Then verify:

- no credentials, `.env`, data, PDFs, or request captures are committed;
- `LICENSE` and `NOTICE` are present;
- README examples match the current CLI/package name;
- the cross-session isolation test passes;
- release identity/configuration tests pass;
- the final image uses a unique semantic version tag;
- `release.yml` is the configured PyPI Trusted Publisher workflow;
- the GitHub environment is named exactly `pypi`;
- both `amd64` and `arm64` manifests are present after publication.

Example:

```bash
git tag v0.3.1
git push origin v0.3.1
```

## Security boundary

The container reduces accidental host access but is not a hardened sandbox. Biomni can execute generated code, access files mounted into the container, and make network requests.

Do not treat queued browser sessions as mutually hostile tenants. Strong multi-user separation requires separate processes/containers or another execution sandbox.
