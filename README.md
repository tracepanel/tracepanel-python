# Tracepanel

[![CI](https://github.com/tracepanel/tracepanel-python/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/tracepanel/tracepanel-python/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tracepanel)](https://pypi.org/project/tracepanel/)
[![Python](https://img.shields.io/pypi/pyversions/tracepanel)](https://pypi.org/project/tracepanel/)
[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://app.codspeed.io/tracepanel/tracepanel-python)

**See what your Python app is really doing.** Tracepanel records each
request, background task and command of your app with its SQL queries, Redis
commands, outgoing HTTP calls, templates, logs and errors. It points out N+1
queries and calls, duplicates, slow SQL and slow HTTP calls, and names the line
of code to fix. You read it in a panel on your pages and in a console.

It runs while you develop, and everything stays on your machine.

[![A 40-second demo: Swagger UI's Try it out on an endpoint, and the panel docked under it with the request's call tree, an N+1 with its query plan, a background task, its profile, and the same call after the fix](https://raw.githubusercontent.com/tracepanel/tracepanel-python/main/assets/demo-video.jpg)](https://tracepanel.dev/video/demo.mp4)

▶ [Watch the 40-second demo](https://tracepanel.dev/video/demo.mp4)

## Works with

| | Supported |
| --- | --- |
| **Python** | 3.11 to 3.15. With Django: 5.2 on Python 3.11 to 3.14, 6.0 on 3.12 to 3.14, 6.1 on 3.12 to 3.15, each pair tested in CI. |
| **Frameworks** | Django 5.2, 6.0 and 6.1, FastAPI, Starlette, Flask, Falcon (WSGI and ASGI). Any other ASGI or WSGI app, such as Litestar, Quart, Bottle or Pyramid, at the request level. Under any ASGI or WSGI server, such as uvicorn or gunicorn, or the framework's own. In Django also signals, cache, email, file storages, `collectstatic`, `migrate` and management commands. |
| **Templates** | Django's and Jinja2. |
| **Databases** | SQLAlchemy 2 and the Django ORM, or a driver directly: sqlite3, aiosqlite, psycopg, psycopg2, asyncpg, PyMySQL, aiomysql, asyncmy, mysqlclient, MySQL Connector, oracledb, pyodbc, pymssql and Snowflake's connector (sync and asyncio). Opening a connection shows as its own span: a Snowflake login, a PostgreSQL or MySQL handshake. EXPLAIN on SQLite, PostgreSQL, MySQL and SQL Server. MongoDB through PyMongo. |
| **APIs** | Django REST framework, Django Ninja, Strawberry, Graphene (through graphene-django), htmx. |
| **Background work** | Celery, Dramatiq, RQ, Huey, Taskiq, FastStream. In Django also django-q2, Django Tasks and Channels. |
| **Clients** | requests and urllib3, httpx, aiohttp, Redis and Valkey, AWS through boto3 and aiobotocore, the OpenAI and Anthropic SDKs. Spans from OpenTelemetry instrumentations join the trace. |
| **Logs** | The lines you write with `logging`, each on the request, task or command that wrote it. |
| **Profiling** | Where a request's or a task's time goes, and its memory when you ask for it. |
| **Tests** | `pytest --tracepanel` fails a test that makes an N+1 or duplicate queries. |

The libraries' versions, the settings and what each integration records are in
the [Python docs](https://tracepanel.dev/docs/python).

## Install

```bash
pip install tracepanel
```

An integration turns on by itself when its library is installed. An extra
installs the library with the lowest version Tracepanel supports, such as
`pip install "tracepanel[fastapi,sqlalchemy]"`: `django`, `fastapi`,
`starlette`, `flask`, `falcon`, `sqlalchemy`, `mongodb`, `drf`, `ninja`,
`strawberry`, `channels`, `celery`, `dramatiq`, `rq`, `huey`, `taskiq`,
`faststream`, `django-q2` and `otel`. For Django,
`pip install django-tracepanel` is the same as `tracepanel[django]`.

## Setup

Tracepanel is on only in development: when `DEBUG` is on, in a debug server,
or under `tracepanel run`. Add it to your app:

```python
# Django: settings.py
INSTALLED_APPS += ["tracepanel.django"]
```

```python
# FastAPI
from fastapi import FastAPI

import tracepanel.fastapi

app = FastAPI(debug=True)
tracepanel.fastapi.install(app)
```

```python
# Flask, started with `flask run --debug`
from flask import Flask

import tracepanel.flask

app = Flask(__name__)
tracepanel.flask.install(app)
```

Open a page of your app and click the badge in the corner to open the panel.
The console at `/__tracepanel__/` holds every request, task and command the
app ran.

## What it looks like

Each request shows its call tree, with the SQL queries, the calls and the
issues in it. Here a FastAPI and SQLAlchemy store lists its products from
Swagger UI, and loads each product's category, brand and reviews in a query of
its own:

![A FastAPI app's Swagger UI with an endpoint run with Try it out and the start of its 200 response, and the panel docked under it with the same request's call tree and its three N+1 queries](https://raw.githubusercontent.com/tracepanel/tracepanel-python/main/assets/panel-call-tree.png)

The panel also lists the requests your pages and tools make with `fetch()`,
here Swagger UI's Try it out. Each issue names the line that ran it and the fix
in your library's terms, such as SQLAlchemy's `joinedload()` or Django's
`select_related()`:

![The same Swagger UI with the panel docked under it on that request's Issues tab: each N+1 with the line it comes from and the fix](https://raw.githubusercontent.com/tracepanel/tracepanel-python/main/assets/panel-issues.png)

## Run without changing your code

`tracepanel run` turns Tracepanel on for one command. The project stays as it
is:

```bash
tracepanel run python manage.py runserver   # Django
tracepanel run uvicorn app.main:app         # FastAPI, Starlette
tracepanel run flask --app app run          # Flask
tracepanel run gunicorn app:app             # Falcon (WSGI)
tracepanel run uvicorn app:app              # Falcon (ASGI)
tracepanel run celery -A app worker         # a worker
tracepanel run taskiq worker app:broker     # a Taskiq worker
```

## Configure

Every setting has a default. To change one, set it in one of these places, and
a later place wins:

1. `tracepanel.toml`, or `[tool.tracepanel]` in `pyproject.toml`
2. the `TRACEPANEL` dict in Django's settings
3. a `TRACEPANEL_<NAME>` variable, such as `TRACEPANEL_SLOW_SQL_MS=50`

```toml
# pyproject.toml
[tool.tracepanel]
slow_sql_ms = 50
n_plus_one_threshold = 5
ignore_paths = ["/health*", "/static/*"]
```

`tracepanel config` prints each setting, its value and where it comes from.
The server's access log leaves out tracepanel's own requests (the panel polls
several times a second): `quiet_access_log = false` keeps them.

## Profile a request

A profile shows where a request's time went in code, as a flame graph in the
console. Tracepanel profiles a request that sends an `X-Tracepanel-Profile: 1`
header, and every request while the panel's Profile button is on (it sets the
`tracepanel_profile=1` cookie). The tasks a profiled request queues are
profiled too, on the worker. `profile = true` profiles every trace, and
`profile_enabled = false` turns profiling off.

A frame that waits rather than computes (a sleep, a socket read, a lock) is
marked as waiting, and an async trace shows what its task awaits instead of
the idle event loop. Every profile counts garbage collections.

A profile counts memory too when asked: `X-Tracepanel-Profile: memory`, the
`tracepanel_profile=memory` cookie, or `profile_memory = true`. It uses
Python's tracemalloc, which slows down every allocation of the whole process
while it runs, so leave it off when you measure time. A trace that isn't
profiled pays for none of this. It reports the peak above the level at the
trace's start, what is still allocated at its end, and the 50 sites that held
the most.

The sampler takes the stack every `profile_interval_ms` (1 by default). Code
that keeps the CPU busy holds Python's GIL, so it samples less often than
that. The [Profile tab](https://tracepanel.dev/docs/panel#profile) shows what
a profile holds.

## Trace your own code

To add your own steps to a request or a task, or to trace a script or a
scheduled job:

```python
import time

import tracepanel


@tracepanel.traced
def rebuild_index() -> None:
    with tracepanel.span("index.load", source="db") as span:
        time.sleep(0.01)
        span.set("rows", 120)
    tracepanel.event("index rebuilt", rows=120)


with tracepanel.trace("rebuild-index"):
    tracepanel.set_attr("tenant", "acme", on_trace=True)
    rebuild_index()
```

`span()` adds a step to the current trace, and `@traced` does the same for a
function. `trace()` starts a new trace. Tracepanel stores keyword attributes
under `app.`, so `source` becomes `app.source`. SQL, logs and errors inside a
span belong to that span. Outside development these calls do nothing, so you
can leave them in.

## In tests

The pytest plugin turns what Tracepanel finds into test failures, so an N+1
query fails CI before it reaches production. It comes with the package and
stays off until you turn it on:

```bash
pytest --tracepanel
```

```toml
# pyproject.toml
[tool.pytest.ini_options]
tracepanel = true
tracepanel_fail_on = ["n+1", "duplicates"]
```

Each test runs as a trace named after the test. A request the test makes
through Django's test client, Starlette's or FastAPI's `TestClient`, httpx's
ASGI transport or Flask's test client is a trace of its own, linked to the
test's in the console. Fixtures run outside the test's trace, so the test
database's setup doesn't count.

When the test passes, the agent looks at its traces. An issue of a kind in
`tracepanel_fail_on` (`n+1` and `duplicates` by default) fails the test with
the issue, its query, the line of code that ran it and a link to the trace:

```text
tracepanel found 1 issue in this test:

N+1: 5 similar queries · SELECT auth_group
  SELECT "auth_group"."name" FROM "auth_group" ... WHERE "user_id" = %s
  at blog/views.py:9 in users
  in GET /users/ → http://127.0.0.1:52811/#/t/01M5VH6BFMFPA7X692DKQV2ANZ
  Load the related rows in one extra query: add `prefetch_related()` ...
```

A mark changes the rules for a test, a class or a module. A fixture and a
context manager count queries inside the test:

```python
import pytest

import tracepanel


@pytest.mark.tracepanel(max_queries=10, allow=["n+1"])
def test_feed(client, tracepanel_trace):
    client.get("/feed/")
    assert tracepanel_trace.queries <= 10
    with tracepanel.max_queries(2):
        client.get("/feed/?page=2")
```

`fail_on` replaces the kinds that fail the test, `allow` takes kinds out of
them, and `max_queries` caps the queries of the test and its requests.
`tracepanel_trace.issues` lists what the agent found in the test's requests
so far.

The plugin starts the project's agent when none is running, as
`tracepanel run` does. Without an agent (`agent = "off"`, or none for the
platform) the tests run without findings, and one warning says so. Query
counts need no agent. Under pytest-xdist every worker sends its tests to the
same agent, and async tests under anyio or pytest-asyncio work the same way.

## Command line

| Command | What it does |
| --- | --- |
| `tracepanel run <command>` | runs a command with Tracepanel on |
| `tracepanel dev` | runs the agent in the foreground, with the console on http://127.0.0.1:8765/ |
| `tracepanel config` | prints the settings in effect and where each comes from |
| `tracepanel clear` | deletes every stored trace, log, query, email and issue |
| `tracepanel mcp` | gives an AI coding agent your traces over MCP |

## Documentation

The documentation is at
[tracepanel.dev/docs/python](https://tracepanel.dev/docs/python): installing,
the integrations, settings, tracing your own code and the command line. The
panel, the console and the issues are the same in every framework, under
[tracepanel.dev/docs](https://tracepanel.dev/docs). Django has a guide of its
own at [tracepanel.dev/docs/django](https://tracepanel.dev/docs/django).

## Links

- [tracepanel](https://github.com/tracepanel/tracepanel): what Tracepanel finds,
  how it works, privacy, and the agent this package installs
- [CHANGELOG.md](CHANGELOG.md): what changed in each version
- [CONTRIBUTING.md](CONTRIBUTING.md): developing this package

## License

Licensed under the [MIT License](LICENSE). The agent it installs,
`tracepanel-agent`, is free to use under the
[FSL-1.1-ALv2](https://github.com/tracepanel/tracepanel/blob/main/LICENSE):
source available, and Apache-2.0 two years after each release.
