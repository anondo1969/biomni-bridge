from __future__ import annotations

import logging
from typing import Any

import gradio as gr

from .adapter import format_trace_markdown
from .config import ConfigError, Settings, scrub_secret_environment
from .errors import describe_model_error, safe_exception_text
from .models import ModelConnectionError, resolve_model_choices, validate_model_connection
from .sessions import SessionRegistry

log = logging.getLogger(__name__)

EXAMPLES = [
    "Plan a CRISPR screen of 100 genes to identify genes involved in T cell activation in response to PD-1 blockade.",
    "Deeply research the potential genetic mechanism of rs6690215.",
    "Identify repurposing drug candidates for myasthenia gravis and explain the evidence.",
    "I have a GWAS hit at rs2155219 for ulcerative colitis. Interpret the potential mechanism.",
]

CSS = """
#app-title h1 { margin-bottom: 0.2rem; }
#runtime-status { opacity: 0.82; }
.trace-box pre, .trace-box code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.gradio-container code { white-space: pre-wrap; }
.connection-ok { border-left: 4px solid var(--primary-500); padding-left: 0.8rem; }
"""


def _session_id(request: gr.Request) -> str:
    session_id = getattr(request, "session_hash", "") or ""
    if not session_id:
        raise RuntimeError("Gradio session ID is unavailable; refresh the page and try again")
    return session_id


def build_demo(settings: Settings) -> gr.Blocks:
    registry = SessionRegistry(ttl_seconds=settings.session_ttl_seconds)
    ui_credentials_enabled = settings.ui_credentials_enabled

    def render_trace(parts: list[str]) -> str:
        return "\n\n---\n\n".join(format_trace_markdown(part) for part in parts[-30:] if part)

    def disabled_controls(status: str):
        return (
            gr.update(choices=[], value=None, interactive=False),
            status,
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
            gr.update(interactive=False),
        )

    def enabled_controls(info):
        return (
            gr.update(choices=list(info.choices), value=info.selected, interactive=True),
            info.status,
            gr.update(interactive=True),
            gr.update(interactive=True),
            gr.update(interactive=True),
            gr.update(interactive=True),
            gr.update(interactive=True),
        )

    def initialize_session(request: gr.Request):
        if not settings.server_credentials_enabled:
            return disabled_controls("Not connected. Enter an API endpoint and key above.")
        info = resolve_model_choices(settings)
        registry.connect(_session_id(request), settings, info.selected)
        return enabled_controls(info)

    def connect_endpoint(base_url: str, api_key: str, request: gr.Request):
        try:
            session_id = _session_id(request)
            # A reconnect attempt intentionally releases the previous adapter/key
            # before validating new credentials. A failed reconnect must not leave
            # an old secret alive behind a disabled UI.
            registry.remove(session_id)
            session_settings = settings.with_ui_credentials(base_url, api_key)
            info = validate_model_connection(session_settings)
            registry.connect(session_id, session_settings, info.selected)
        except (ConfigError, ModelConnectionError, RuntimeError) as exc:
            status = f"Connection failed: {safe_exception_text(exc, api_key)}"
            controls = disabled_controls(status)
            return (status, gr.update(value=""), *controls)

        status = f"Connected to {session_settings.base_url}. The API key is held only in server session memory."
        return (status, gr.update(value=""), *enabled_controls(info))

    def disconnect_endpoint(request: gr.Request):
        registry.remove(_session_id(request))
        status = "Disconnected. The session adapter and credential references were released."
        controls = disabled_controls(status)
        return (status, gr.update(value=""), [], "", gr.update(value=None, visible=False), *controls)

    def run_task(message: str, history: list[dict[str, Any]] | None, model: str | None, request: gr.Request):
        message = (message or "").strip()
        history = list(history or [])
        try:
            runtime = registry.require(_session_id(request))
        except RuntimeError as exc:
            yield history, "", str(exc), gr.update(interactive=False)
            return

        if not message:
            yield history, "", "Enter a task first.", gr.update(interactive=True)
            return

        adapter = runtime.adapter
        adapter.set_model(model or "")
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": "Working on the task…"})
        trace_parts: list[str] = []
        yield history, "", "Starting Biomni…", gr.update(interactive=False)

        try:
            final_answer: str | None = None
            for update in adapter.stream(message):
                if update.trace and (not trace_parts or trace_parts[-1] != update.trace):
                    trace_parts.append(update.trace)
                final_answer = update.final_answer or final_answer
                history[-1] = {
                    "role": "assistant",
                    "content": final_answer or "Working on the task… See the execution trace below.",
                }
                yield (
                    history,
                    render_trace(trace_parts),
                    f"Running with {adapter.requested_model}",
                    gr.update(interactive=False),
                )

            if not final_answer:
                final_answer = "Biomni finished, but no final answer could be extracted. See the execution trace."
            history[-1] = {"role": "assistant", "content": final_answer}
            yield (
                history,
                render_trace(trace_parts),
                f"Completed with {adapter.built_model}",
                gr.update(interactive=True),
            )
        except Exception as exc:  # noqa: BLE001 - surface agent failures to the current user
            error_info = describe_model_error(
                exc,
                base_url=runtime.settings.base_url,
                secret=runtime.settings.api_key,
            )
            log.error("Biomni task failed: %s", error_info.message)
            history[-1] = {
                "role": "assistant",
                "content": f"**Biomni request failed.**\n\n{error_info.message}",
            }
            yield history, render_trace(trace_parts), error_info.status, gr.update(interactive=True)

    def change_model(model: str | None, request: gr.Request) -> str:
        try:
            runtime = registry.require(_session_id(request))
        except RuntimeError as exc:
            return str(exc)
        return runtime.adapter.set_model(model or "")

    def refresh_models(current_model: str | None, request: gr.Request):
        try:
            runtime = registry.require(_session_id(request))
        except RuntimeError as exc:
            return gr.update(interactive=False), str(exc)
        info = resolve_model_choices(runtime.settings)
        choices = list(info.choices)
        selected = current_model if current_model in choices else info.selected
        if current_model and current_model not in choices:
            choices.insert(0, current_model)
            selected = current_model
        return gr.update(choices=choices, value=selected), info.status

    def export_pdf(request: gr.Request):
        try:
            runtime = registry.require(_session_id(request))
            return gr.update(value=str(runtime.adapter.export_pdf()), visible=True)
        except Exception as exc:  # noqa: BLE001
            runtime = registry.get(_session_id(request))
            secret = runtime.settings.api_key if runtime is not None else ""
            gr.Warning(f"PDF export failed: {safe_exception_text(exc, secret)}")
            return gr.update(visible=False)

    def clear_session(model: str | None, request: gr.Request):
        try:
            runtime = registry.require(_session_id(request))
        except RuntimeError as exc:
            return [], "", str(exc), gr.update(value=None, visible=False)
        runtime.adapter.reset(model or "")
        status = f"Fresh Biomni run state; next task will use {model}." if model else "Fresh run state; select a model."
        return [], "", status, gr.update(value=None, visible=False)

    def cleanup_session(request: gr.Request) -> None:
        session_id = getattr(request, "session_hash", "") or ""
        registry.remove(session_id)

    with gr.Blocks(css=CSS, title="Biomni Bridge", delete_cache=(3600, 3600)) as demo:
        gr.Markdown(
            "# Biomni Bridge\n"
            "Bridge Biomni to your own OpenAI-compatible model endpoint.",
            elem_id="app-title",
        )
        gr.Markdown(
            "**Biomni:** `0.0.8`  ·  "
            f"**Credential mode:** `{settings.credential_mode}`  ·  "
            f"**Data download:** `{'skipped' if settings.skip_data_download else 'enabled'}`\n\n"
            "Each browser session gets its own lazy Biomni adapter. UI-provided API keys are not written to disk, "
            "returned to the browser as state, or exported to generated subprocess environments.",
            elem_id="runtime-status",
        )

        with gr.Accordion(
            "Connect model endpoint",
            open=not settings.server_credentials_enabled,
            visible=ui_credentials_enabled,
        ):
            gr.Markdown(
                "Enter your own OpenAI-compatible endpoint and API key. **Connect** validates `GET /models`; "
                "the password field is then cleared. UI endpoint validation blocks private/internal targets by default."
            )
            with gr.Row():
                endpoint_input = gr.Textbox(
                    value=settings.base_url,
                    label="Base API URL",
                    placeholder="https://models.example.org/v1",
                    scale=4,
                )
                api_key_input = gr.Textbox(
                    value="",
                    label="API key",
                    type="password",
                    placeholder="Paste your scoped/revocable API key",
                    scale=4,
                )
                connect = gr.Button("Connect", variant="primary", scale=1)
                disconnect = gr.Button("Disconnect", scale=1)
            connection_status = gr.Markdown(
                "Using server-managed credentials." if settings.server_credentials_enabled else "**Not connected.**"
            )

        with gr.Row():
            model = gr.Dropdown(
                choices=[],
                value=None,
                allow_custom_value=True,
                label="Model",
                info="Loaded from GET /models after connecting; custom IDs are allowed.",
                interactive=False,
                scale=3,
            )
            refresh = gr.Button("Refresh models", interactive=False, scale=1)
            model_status = gr.Textbox(
                value="Connecting…" if settings.server_credentials_enabled else "Connect an endpoint first.",
                label="Status",
                interactive=False,
                scale=3,
            )

        chatbot = gr.Chatbot(type="messages", label="Task history", height=520, show_copy_button=True)

        with gr.Row():
            task = gr.Textbox(
                lines=3,
                label="Task",
                placeholder="Describe a biomedical research task…",
                interactive=False,
                scale=5,
            )
            submit = gr.Button("Run", variant="primary", interactive=False, scale=1)

        gr.Examples(examples=EXAMPLES, inputs=task, label="Example tasks")

        with gr.Accordion("Writing a good Biomni task", open=False):
            gr.Markdown(
                "- Biomni is most useful for multi-step research work such as analysis, protocol design, "
                "or mechanism investigation.\n"
                "- State important constraints, thresholds, input files, and desired outputs explicitly.\n"
                "- Biomni can write and run code inside this container, so give it the same boundaries "
                "you would give a research assistant.\n"
                "- Treat API keys as credentials: use scoped/revocable API keys, not administrative secrets."
            )

        with gr.Accordion("Execution trace", open=False):
            trace = gr.Markdown(
                value="_No execution trace yet._",
                elem_classes=["trace-box"],
            )

        with gr.Row():
            export = gr.Button("Export last run PDF", interactive=False)
            clear = gr.Button("Clear Biomni run", interactive=False)
        pdf_file = gr.File(label="Generated PDF", visible=False, interactive=False)

        control_outputs = [model, model_status, refresh, task, submit, export, clear]
        demo.load(initialize_session, outputs=control_outputs, api_name=False)

        if ui_credentials_enabled:
            connect.click(
                connect_endpoint,
                inputs=[endpoint_input, api_key_input],
                outputs=[connection_status, api_key_input, *control_outputs],
                api_name=False,
            )
            disconnect.click(
                disconnect_endpoint,
                outputs=[connection_status, api_key_input, chatbot, trace, pdf_file, *control_outputs],
                api_name=False,
            )

        model.change(change_model, inputs=model, outputs=model_status, api_name=False)
        refresh.click(refresh_models, inputs=model, outputs=[model, model_status], api_name=False)

        submit_event = submit.click(
            run_task,
            inputs=[task, chatbot, model],
            outputs=[chatbot, trace, model_status, submit],
            api_name=False,
        )
        enter_event = task.submit(
            run_task,
            inputs=[task, chatbot, model],
            outputs=[chatbot, trace, model_status, submit],
            api_name=False,
        )
        submit_event.then(lambda: "", outputs=task, api_name=False)
        enter_event.then(lambda: "", outputs=task, api_name=False)

        export.click(export_pdf, outputs=pdf_file, api_name=False)
        clear.click(
            clear_session,
            inputs=model,
            outputs=[chatbot, trace, model_status, pdf_file],
            api_name=False,
        )
        demo.unload(cleanup_session)

    return demo


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    # In env/auto mode, load the key once into immutable Settings then remove it
    # from the process environment. In ui mode there is normally no key in the
    # environment at all; this remains a defense-in-depth scrub.
    scrub_secret_environment()
    settings.prepare_directories()

    demo = build_demo(settings)
    demo.queue(default_concurrency_limit=1).launch(
        server_name=settings.server_name,
        server_port=settings.server_port,
        show_error=False,
        show_api=False,
        allowed_paths=[str(settings.output_dir.resolve())],
    )


if __name__ == "__main__":
    main()
