from pathlib import Path

import pytest
from django.test import Client

from tracepanel.console import app


@pytest.fixture
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in UI bundle, so these tests don't need the real build."""
    (tmp_path / "ui" / "assets").mkdir(parents=True)
    (tmp_path / "ui" / "index.html").write_text("<!doctype html><title>tracepanel</title>")
    (tmp_path / "ui" / "assets" / "index-abc123.js").write_text("console.log(1)")
    (tmp_path / "widget.js").write_text("// widget")
    monkeypatch.setattr(app, "STATIC", tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    ("path", "content_type", "cache"),
    [
        ("/__tracepanel__/", "text/html", "no-cache"),
        ("/__tracepanel__/traces/anything", "text/html", "no-cache"),
        ("/__tracepanel__/widget.js", "text/javascript", "no-cache"),
        ("/__tracepanel__/assets/index-abc123.js", "text/javascript", "public, max-age=31536000, immutable"),
    ],
)
@pytest.mark.django_db
def test_assets_and_caching(client: Client, built: Path, path: str, content_type: str, cache: str) -> None:
    response = client.get(path)
    # A FileResponse holds its file open until closed, as a server closes it (request_finished: hence django_db).
    response.close()

    assert (response.status_code, response["Content-Type"], response["Cache-Control"]) == (200, content_type, cache)


@pytest.mark.parametrize("path", ["/__tracepanel__/../settings.py", "/__tracepanel__/assets/../../widget.js"])
def test_no_path_traversal(client: Client, built: Path, path: str) -> None:
    assert client.get(path).status_code == 404
