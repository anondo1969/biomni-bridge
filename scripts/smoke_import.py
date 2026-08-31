from __future__ import annotations

import importlib
import importlib.metadata as metadata
import importlib.util

EXPECTED = {
    "biomni": "0.0.8",
    "gradio": "5.39.0",
    "langgraph": "0.3.18",
    "python-dotenv": "1.2.2",
    "requests": "2.33.1",
    "biopython": "1.88",
}

MODULES = (
    "biomni",
    "biomni.agent",
    "biomni.utils",
    "gradio",
    "langgraph",
    "biomni_bridge.adapter",
    "biomni_bridge.app",
    "biomni_bridge.config",
    "biomni_bridge.endpoint_check",
    "biomni_bridge.endpoint_diagnose",
    "biomni_bridge.llm_compat",
    "biomni_bridge.models",
    "biomni_bridge.sessions",
)

SCIENTIFIC_MODULES = (
    "pandas",
    "numpy",
    "scipy",
    "sklearn",
    "matplotlib",
    "statsmodels",
    "networkx",
    "bs4",
    "lxml",
    "transformers",
    "Bio",
)

for module in MODULES + SCIENTIFIC_MODULES:
    importlib.import_module(module)

for package, expected in EXPECTED.items():
    actual = metadata.version(package)
    if actual != expected:
        raise SystemExit(f"Expected {package}=={expected}, found {actual}")

# Guard the exact Biomni 0.0.8 surface used by this wrapper. go_stream() and
# save_conversation_history() are the public integration points; the markdown
# helper is the one deliberate private seam needed to avoid SIGALRM in Gradio
# worker threads during PDF export.
from biomni.agent import A1  # noqa: E402
from biomni.utils import convert_markdown_to_pdf  # noqa: F401,E402

for name in ("go_stream", "save_conversation_history", "_generate_markdown_content"):
    if not hasattr(A1, name):
        raise SystemExit(f"Biomni 0.0.8 compatibility surface missing A1.{name}")

# Importing WeasyPrint alone is not enough: missing Pango/Harfbuzz libraries can
# fail only when a document is rendered. Generate a tiny PDF to exercise the
# real native-library path used by Biomni exports.
from weasyprint import HTML  # noqa: E402

pdf = HTML(string="<p>Biomni Bridge smoke test</p>").write_pdf()
if not pdf.startswith(b"%PDF"):
    raise SystemExit("WeasyPrint smoke render did not produce a PDF")

if importlib.util.find_spec("fastmcp") is not None:
    raise SystemExit("fastmcp must not be present in the default image")

print(f"Biomni Bridge {metadata.version('biomni-bridge')} / Biomni 0.0.8 runtime compatibility checks OK")
