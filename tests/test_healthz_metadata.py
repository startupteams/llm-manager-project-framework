"""§17: /healthz must expose build identity (git_sha, build_time, schema_version)."""
import os
import sys
import json
import tempfile
from unittest import mock

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "service", "app")
if APP not in sys.path:
    sys.path.insert(0, APP)


def _load_main():
    import main
    return main


def test_healthz_includes_build_metadata(tmp_path):
    m = _load_main()
    man = {"git_sha": "abc123def456", "build_time": "2026-09-27T04:00:00Z"}
    manfile = tmp_path / "release_manifest.json"
    manfile.write_text(json.dumps(man))
    with mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(man))):
        with mock.patch.object(m, "_schema_version", return_value="001"):
            resp = m.healthz()
    assert resp["ok"] is True
    assert resp["git_sha"] == "abc123def456"
    assert resp["build_time"] == "2026-09-27T04:00:00Z"
    assert resp["schema_version"] == "001"


def test_healthz_degrades_gracefully_without_manifest():
    m = _load_main()
    real_open = open
    def fake_open(path, *a, **k):
        if "release_manifest" in str(path):
            raise FileNotFoundError(path)
        return real_open(path, *a, **k)
    with mock.patch("builtins.open", side_effect=fake_open):
        with mock.patch.object(m, "_schema_version", return_value="unknown"):
            resp = m.healthz()
    assert resp["ok"] is True
    assert resp["git_sha"] == "unknown"
    assert resp["build_time"] == "unknown"


def test_schema_version_query_monkeypatched():
    """The _schema_version helper reads MAX(version) from schema_migrations."""
    import inspect
    src = inspect.getsource(_load_main()._schema_version)
    assert "schema_migrations" in src
    assert "MAX(version)" in src
