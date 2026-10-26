import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from dirty_equals import IsFloat, IsInt, IsPartialDict, IsStr
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage, storages
from django.core.management import call_command
from jsonschema import Draft202012Validator
from pytest_django.fixtures import Settings

from tracepanel._compat import override
from tracepanel.core.context import start_trace
from tracepanel.core.model import Span, Trace
from tracepanel.django import collectstatic
from tracepanel.integrations import http_clients
from tracepanel.storage.transport import complete

http_clients.install()

ID = IsStr(regex=r"[0-9A-Z]{26}")
FILESYSTEM = "django.core.files.storage.filesystem.FileSystemStorage"


def origin(test: str) -> IsStr:
    return IsStr(regex=rf"tests/django/test_storage\.py:\d+ in {test}")


def tree(trace: Trace) -> list[tuple[str, str, str | None]]:
    """(kind, name, parent name) of every span under the root, in order."""
    spans = {span.id: span for span in trace.spans}
    return [
        (span.kind, span.name, spans[span.parent_id].name if span.parent_id in spans else None)
        for span in trace.spans[1:]
    ]


@pytest.fixture
def media(settings: Settings, tmp_path: Path) -> Storage:
    settings.STORAGES = {
        "default": {"BACKEND": FILESYSTEM, "OPTIONS": {"location": str(tmp_path / "media")}},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    return storages["default"]


def test_filesystem_calls_are_spans(media: Storage, validator: Draft202012Validator) -> None:
    with start_trace("command", "avatars") as trace:
        name = media.save("avatars/1.png", ContentFile(b"png!"))
        media.exists(name)
        with media.open(name) as file:
            file.read()
        # Joins strings on the local file system: not a call.
        media.url(name)
        media.listdir("avatars")
        media.delete(name)

    validator.validate(complete(trace).to_dict())
    service = {"storage.alias": "default", "storage.backend": FILESYSTEM, "storage.system": "filesystem"}
    called_here = origin("test_filesystem_calls_are_spans")

    assert [span.to_dict() for span in trace.spans[1:]] == [
        {
            "id": ID,
            "parent_id": trace.spans[0].id,
            "kind": "storage",
            "name": "storage save avatars/1.png",
            "start": IsInt(),
            "duration": IsInt(ge=0),
            "status": "ok",
            "attrs": {
                **service,
                "storage.op": "save",
                "storage.path": "avatars/1.png",
                "storage.bytes": 4,
                "code.origin": called_here,
            },
        },
        {
            "id": ID,
            "parent_id": trace.spans[0].id,
            "kind": "storage",
            "name": "storage exists avatars/1.png",
            "start": IsInt(),
            "duration": IsInt(ge=0),
            "status": "ok",
            "attrs": {
                **service,
                "storage.op": "exists",
                "storage.path": "avatars/1.png",
                "code.origin": called_here,
                "storage.exists": True,
            },
        },
        {
            "id": ID,
            "parent_id": trace.spans[0].id,
            "kind": "storage",
            "name": "storage open avatars/1.png",
            "start": IsInt(),
            "duration": IsInt(ge=0),
            "status": "ok",
            "attrs": {
                **service,
                "storage.op": "open",
                "storage.path": "avatars/1.png",
                "code.origin": called_here,
                "storage.bytes": 4,
            },
        },
        {
            "id": ID,
            "parent_id": trace.spans[0].id,
            "kind": "storage",
            "name": "storage listdir avatars",
            "start": IsInt(),
            "duration": IsInt(ge=0),
            "status": "ok",
            "attrs": {
                **service,
                "storage.op": "listdir",
                "storage.path": "avatars",
                "code.origin": called_here,
                "storage.dirs": 0,
                "storage.files": 1,
            },
        },
        {
            "id": ID,
            "parent_id": trace.spans[0].id,
            "kind": "storage",
            "name": "storage delete avatars/1.png",
            "start": IsInt(),
            "duration": IsInt(ge=0),
            "status": "ok",
            "attrs": {**service, "storage.op": "delete", "storage.path": "avatars/1.png", "code.origin": called_here},
        },
    ]


def test_a_save_under_a_taken_name_says_the_name_it_chose(media: Storage) -> None:
    media.save("avatars/1.png", ContentFile(b"png!"))

    with start_trace("command", "avatars") as trace:
        media.save("avatars/1.png", ContentFile(b"png?"))

    (span,) = trace.spans[1:]

    assert span.attrs["storage.saved_as"] == IsStr(regex=r"avatars/1_\w{7}\.png")


def test_a_failed_call_is_an_error(media: Storage) -> None:
    with start_trace("command", "avatars") as trace, pytest.raises(FileNotFoundError):
        media.open("avatars/missing.png")

    assert [(span.name, span.status) for span in trace.spans[1:]] == [("storage open avatars/missing.png", "error")]


class Avatars(FileSystemStorage):
    """A project's own storage, created after tracepanel was installed: its methods call Django's."""

    @override
    def exists(self, name: str) -> bool:
        return super().exists(name)

    @override
    def get_available_name(self, name: str, max_length: int | None = None) -> str:
        return super().get_available_name(f"avatars/{name}", max_length)


def test_a_subclass_is_counted_once_per_call(tmp_path: Path) -> None:
    avatars = Avatars(location=str(tmp_path))

    with start_trace("command", "avatars") as trace:
        # save() asks exists() for a free name, Avatars.exists() asks FileSystemStorage's: one save.
        avatars.save("1.png", ContentFile(b"png!"))
        avatars.exists("avatars/1.png")
        avatars.get_modified_time("avatars/1.png")
    by_name = {span.name: span.attrs for span in trace.spans[1:]}

    assert tree(trace) == [
        ("storage", "storage save 1.png", "avatars"),
        ("storage", "storage exists avatars/1.png", "avatars"),
        ("storage", "storage get_modified_time avatars/1.png", "avatars"),
    ]

    assert by_name["storage save 1.png"] == {
        "storage.alias": None,
        "storage.backend": "tests.django.test_storage.Avatars",
        "storage.system": "filesystem",
        "storage.op": "save",
        "storage.path": "1.png",
        "storage.bytes": 4,
        "code.origin": origin("test_a_subclass_is_counted_once_per_call"),
        "storage.saved_as": "avatars/1.png",
    }


def test_outside_a_trace_nothing_is_recorded(media: Storage) -> None:
    assert media.save("avatars/1.png", ContentFile(b"png!")) == "avatars/1.png"
    assert media.exists("avatars/1.png") is True


# ---------- a remote storage: its HTTP calls inside its spans ----------

bucket: dict[str, bytes] = {}


class Bucket(BaseHTTPRequestHandler):
    """An object store's API, enough for a storage: PUT, GET, HEAD, DELETE on /<name>."""

    def _reply(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_PUT(self) -> None:
        bucket[self.path] = self.rfile.read(int(self.headers["Content-Length"]))
        self._reply(200)

    def do_GET(self) -> None:
        self._reply(200, bucket[self.path]) if self.path in bucket else self._reply(404)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_DELETE(self) -> None:
        bucket.pop(self.path, None)
        self._reply(204)

    @override
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002, ANN401 (http.server's signature)
        pass


@pytest.fixture(scope="module")
def endpoint() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Bucket)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


class BucketStorage(Storage):
    """A storage over HTTP, as django-storages' S3Storage is over boto3."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def _open(self, name: str, mode: str = "rb") -> ContentFile[bytes]:
        import requests  # noqa: PLC0415 (the clients group)

        return ContentFile(requests.get(f"{self.endpoint}/{name}", timeout=5).content, name=name)

    def _save(self, name: str, content: Any) -> str:  # noqa: ANN401 (a File)
        import requests  # noqa: PLC0415

        requests.put(f"{self.endpoint}/{name}", data=content.read(), timeout=5)
        return name

    @override
    def exists(self, name: str) -> bool:
        import requests  # noqa: PLC0415

        return requests.head(f"{self.endpoint}/{name}", timeout=5).status_code == 200

    @override
    def delete(self, name: str) -> None:
        import requests  # noqa: PLC0415

        requests.delete(f"{self.endpoint}/{name}", timeout=5)

    @override
    def url(self, name: str | None) -> str:
        return f"{self.endpoint}/{name}?X-Amz-Signature=0f3a"


# Where S3Storage lives: the system is S3's.
BucketStorage.__module__ = "storages.backends.s3"


def test_a_remote_storage_holds_its_http_calls(endpoint: str, settings: Settings) -> None:
    pytest.importorskip("requests")
    settings.STORAGES = {
        "default": {"BACKEND": FILESYSTEM},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
        "uploads": {"BACKEND": "tests.django.test_storage.BucketStorage", "OPTIONS": {"endpoint": endpoint}},
    }
    uploads = storages["uploads"]
    bucket.clear()

    with start_trace("command", "upload") as trace:
        uploads.save("invoices/7.pdf", ContentFile(b"%PDF"))
        uploads.open("invoices/7.pdf").read()
        uploads.url("invoices/7.pdf")
        uploads.delete("invoices/7.pdf")
    save, *_ = (span for span in trace.spans if span.name == "storage save invoices/7.pdf")

    assert tree(trace) == [
        ("storage", "storage save invoices/7.pdf", "upload"),
        # save() asking exists() for a free name: no span of its own, its request inside the save.
        ("http.client", "HEAD 127.0.0.1/invoices/7.pdf", "storage save invoices/7.pdf"),
        ("http.client", "PUT 127.0.0.1/invoices/7.pdf", "storage save invoices/7.pdf"),
        ("storage", "storage open invoices/7.pdf", "upload"),
        ("http.client", "GET 127.0.0.1/invoices/7.pdf", "storage open invoices/7.pdf"),
        # Signed: a call, though it makes no request.
        ("storage", "storage url invoices/7.pdf", "upload"),
        ("storage", "storage delete invoices/7.pdf", "upload"),
        ("http.client", "DELETE 127.0.0.1/invoices/7.pdf", "storage delete invoices/7.pdf"),
    ]

    assert save.attrs == {
        "storage.alias": "uploads",
        "storage.backend": "storages.backends.s3.BucketStorage",
        "storage.system": "s3",
        "storage.op": "save",
        "storage.path": "invoices/7.pdf",
        "storage.bytes": 4,
        "code.origin": origin("test_a_remote_storage_holds_its_http_calls"),
    }


# ---------- collectstatic ----------

FILES = 14


@pytest.fixture
def static(settings: Settings, tmp_path: Path) -> Path:
    source = tmp_path / "assets"
    (source / "css").mkdir(parents=True)
    (source / "img").mkdir()
    (source / "img" / "logo.png").write_bytes(b"png!")
    for i in range(FILES - 2):
        (source / "css" / f"page{i}.css").write_text(f".page{i} {{ color: red; }}")
    (source / "css" / "app.css").write_text('body { background: url("../img/logo.png"); }')
    settings.STATICFILES_DIRS = [str(source)]
    settings.STATIC_ROOT = str(tmp_path / "collected")
    settings.STORAGES = {
        "default": {"BACKEND": FILESYSTEM},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
    }
    return source


def blocks(trace: Trace, phase: Span) -> list[Span]:
    return [span for span in trace.spans if span.parent_id == phase.id and not span.name.startswith("storage ")]


def test_collectstatic_shows_its_phases_and_slowest_files(
    static: Path, traces: list[Trace], validator: Draft202012Validator
) -> None:
    del static
    call_command("collectstatic", interactive=False, verbosity=0)
    (trace,) = traces
    validator.validate(trace.to_dict())
    root = trace.spans[0]
    copy, post = (span for span in trace.spans if span.parent_id == root.id and span.name.startswith("collectstatic"))
    copies = blocks(trace, copy)
    by_id = {span.id: span for span in trace.spans}

    assert trace.attrs == IsPartialDict(
        {
            "command.name": "collectstatic",
            "collectstatic.copied": FILES,
            "collectstatic.linked": 0,
            "collectstatic.unmodified": 0,
            "collectstatic.post_processed": FILES,
        }
    )

    assert (copy.name, copy.attrs) == (
        "collectstatic copy",
        {
            "collectstatic.phase": "copy",
            "collectstatic.files": FILES,
            "collectstatic.files_ms": IsFloat(ge=0),
            # The finder listing the source directories.
            "collectstatic.storage_calls": 3,
            "collectstatic.find_ms": IsFloat(ge=0),
        },
    )
    assert (post.name, post.attrs) == (
        "collectstatic post-process",
        {
            "collectstatic.phase": "post-process",
            # Each file, and saving the manifest after the last.
            "collectstatic.files": FILES + 1,
            "collectstatic.files_ms": IsFloat(ge=0),
            "collectstatic.storage_calls": 0,
        },
    )

    # The slowest files only, each with its storage calls.
    assert len(copies) == collectstatic.SLOWEST
    assert {span.attrs["collectstatic.result"] for span in copies} == {"copied"}
    assert {by_id[str(span.parent_id)].name for span in trace.spans if span.name.startswith("storage save")} <= {
        *(span.name for span in copies),
        *(span.name for span in blocks(trace, post)),
    }
    assert len(blocks(trace, post)) == collectstatic.SLOWEST

    # Phases and files are functions, the calls under them storage spans.
    assert {span.kind for span in (copy, post, *copies, *blocks(trace, post))} == {"function"}
    assert {span.kind for span in trace.spans if span.name.startswith("storage ")} == {"storage"}


def test_collectstatic_counts_unmodified_files(static: Path, traces: list[Trace]) -> None:
    del static
    call_command("collectstatic", interactive=False, verbosity=0, post_process=False)
    call_command("collectstatic", interactive=False, verbosity=0, post_process=False)
    first, second = traces
    phases = [span.name for span in second.spans if span.name.startswith("collectstatic")]

    assert (first.attrs["collectstatic.copied"], first.attrs["collectstatic.unmodified"]) == (FILES, 0)

    assert (second.attrs["collectstatic.copied"], second.attrs["collectstatic.unmodified"]) == (0, FILES)
    assert phases == ["collectstatic copy"]
    assert {span.attrs.get("collectstatic.result") for span in second.spans if span.name.startswith("copy ")} == {
        "unmodified"
    }


def test_collectstatic_clear_is_a_phase(static: Path, traces: list[Trace]) -> None:
    del static
    call_command("collectstatic", interactive=False, verbosity=0, post_process=False)
    call_command("collectstatic", interactive=False, verbosity=0, post_process=False, clear=True)
    clear = next(span for span in traces[1].spans if span.name == "collectstatic clear")

    assert clear.attrs == {
        "collectstatic.phase": "clear",
        "collectstatic.files": 0,
        "collectstatic.files_ms": IsFloat(ge=0),
        # exists() and listdir() of each directory, a delete for each file: the slowest kept.
        "collectstatic.storage_calls": IsInt(gt=FILES),
    }
    assert len([span for span in traces[1].spans if span.parent_id == clear.id]) == collectstatic.SLOWEST
