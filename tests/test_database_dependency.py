from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_biopython_is_pinned_for_biomni_database_tools() -> None:
    requirements = (ROOT / "requirements.txt").read_text()
    assert "biopython==1.88" in requirements


def test_runtime_smoke_checks_biopython() -> None:
    smoke = (ROOT / "scripts" / "smoke_import.py").read_text()
    assert '"biopython": "1.88"' in smoke
    assert '"Bio"' in smoke
