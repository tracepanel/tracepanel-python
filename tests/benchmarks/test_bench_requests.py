"""Benchmarks of whole requests through the integrations: what tracepanel adds to every request an app serves."""

from collections.abc import Generator
from typing import Any
from wsgiref.util import setup_testing_defaults

import pytest
from django.contrib.auth.models import User
from django.test import Client
from pytest_codspeed import BenchmarkFixture

import tracepanel.wsgi
from tests.benchmarks.conftest import Discard
from tracepanel.core import conf


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/", "/health/", "/n-plus-one/", "/duplicates/", "/users-page/", "/logs/"])
def test_django_request(
    benchmark: BenchmarkFixture, client: Client, discard: Discard, users: list[User], path: str
) -> None:
    client.get(path)  # warm up: URL resolving, templates, statement caches
    benchmark(client.get, path)
    assert discard.submitted > 0 or path == "/health/"


def _app(environ: dict[str, Any], start_response: Any) -> list[bytes]:  # noqa: ANN401
    del environ
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
    return [b"ok"]


@pytest.fixture
def wsgi_app(discard: Discard) -> Generator[tracepanel.wsgi.WSGIApp, None, None]:
    del discard
    previous = conf._framework  # noqa: SLF001
    yield tracepanel.wsgi.install(_app, enabled=True)
    conf.use_source(previous)


def test_wsgi_request(benchmark: BenchmarkFixture, wsgi_app: tracepanel.wsgi.WSGIApp) -> None:
    def start_response(status: str, headers: list[tuple[str, str]], exc_info: object = None) -> None:
        del status, headers, exc_info

    @benchmark
    def run() -> None:
        environ: dict[str, Any] = {"PATH_INFO": "/orders/5"}
        setup_testing_defaults(environ)
        b"".join(wsgi_app(environ, start_response))
