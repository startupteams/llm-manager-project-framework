"""E4 test — release venv dependency reproducibility (window-4 plan §7).

Proves the Phase-E dependency-import gate logic: a venv satisfying the
freeze-file contract passes; a venv missing a required module (the window-3
sqlalchemy incident) fails BEFORE cutover.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

REQUIRED = {
    "sqlalchemy": "ORM_WRITE_CUTOVER",
    "alembic": "ARM migrations",
    "psycopg2": "legacy write paths",
    "httpx": "FastAPI stack",
    "uvicorn": "service entrypoint",
    "fastapi": "service entrypoint",
}

PROBE = chr(10).join([
    "import importlib, sys",
    f"required = {sorted(REQUIRED)}",
    "missing = []",
    "for mod in required:",
    "    try:",
    "        importlib.import_module(mod)",
    "    except Exception:",
    "        missing.append(mod)",
    "if missing:",
    '    print(", ".join(missing)); sys.exit(1)',
    'print("OK")',
])


def _run_probe(python_bin: str) -> subprocess.CompletedProcess:
    return subprocess.run([python_bin, "-c", PROBE], capture_output=True, text=True)


def test_freeze_contract_probe_fails_on_missing_dep(tmp_path):
    """A venv WITHOUT sqlalchemy must FAIL the probe (window-3 incident class)."""
    import venv as _venv

    env = str(tmp_path / "bare")
    _venv.create(env, with_pip=False)
    py = str(Path(env) / "bin" / "python3")
    r = _run_probe(py)
    assert r.returncode != 0
    assert "sqlalchemy" in r.stdout


def test_freeze_contract_probe_passes_on_release_style_venv(tmp_path):
    """E4 clean-venv proof: fresh venv + the 6 required deps installed → probe passes."""
    import venv as _venv

    env = str(tmp_path / "withdeps")
    _venv.create(env, with_pip=True)
    py = str(Path(env) / "bin" / "python3")
    test_deps = ["psycopg2-binary" if d == "psycopg2" else d for d in sorted(REQUIRED)]
    subprocess.run([py, "-m", "pip", "install", "--quiet", *test_deps],
                   capture_output=True, text=True, check=True, timeout=600)
    r = _run_probe(py)
    assert r.returncode == 0, f"probe failed on prepared env:\n{r.stdout}\n{r.stderr}"
