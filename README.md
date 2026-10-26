# Tracepanel

[![CI](https://github.com/tracepanel/tracepanel-python/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/tracepanel/tracepanel-python/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/tracepanel)](https://pypi.org/project/tracepanel/)
[![Python](https://img.shields.io/pypi/pyversions/tracepanel)](https://pypi.org/project/tracepanel/)
[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://app.codspeed.io/tracepanel/tracepanel-python)

**See what your app does on every request.** Tracepanel records each request,
background task and command of your Python app with its SQL queries, logs and
errors. It points out N+1 queries, duplicate queries and slow SQL, and names the
line of code to fix. You read it in a panel on your pages and in a console.

It runs while you develop, and everything stays on your machine.

## Install

```bash
pip install tracepanel
```

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

Open a page of your app and click the badge in the corner. On a page that
loads each post's author in its own query, the panel shows
`SELECT auth_user ×50` marked N+1, the line of code that ran it, and the
`select_related()` that turns 50 queries into one. The console at
`/__tracepanel__/` holds every request, task and command the app ran.

Tracepanel also traces Celery, Dramatiq, RQ, Huey, FastStream, SQLAlchemy,
Strawberry, Graphene, Django REST framework, Django Ninja, Channels, django-q2
and htmx when they are installed. SQL you run
through a database driver without an ORM is traced too: sqlite3, psycopg,
psycopg2, asyncpg, the MySQL drivers, oracledb, pyodbc and pymssql. So are
the HTTP calls your app makes with requests, httpx, aiohttp or boto3, and its
Redis commands. It reads the log lines you write with `logging` and attaches
each one to the request, task or command that wrote it. Your logging
configuration stays as it is.

In Django it shows each signal sent, its sender, the line that sent it and
the receivers that ran, each with its own time, queries and errors.
Each call a file storage makes to its backend is a span, on the local disk
or S3, and `collectstatic` shows its phases and its slowest files.

For Django, `pip install django-tracepanel` installs the same package.

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
console. The SDK samples the request's stack every `profile_interval_ms`
(1 by default, so a short request still has a
profile to read) while it runs. It profiles a request that sends an
`X-Tracepanel-Profile: 1` header, and every request that carries the
`tracepanel_profile=1` cookie, which the panel's Profile toggle sets while it
is on. The tasks a profiled request queues are profiled too, on the worker
(Django Tasks aside: they carry nothing to it). `profile = true` profiles
every trace, tasks included, and `profile_enabled = false` turns profiling
off.

Code that keeps the CPU busy holds Python's GIL, so the sampler runs less
often than the interval asks. A frame that waits rather than computes (a
sleep, a socket read, a lock, the event loop's select) is marked as waiting,
and each stack says in which 10 ms of the trace it was seen. An async trace
is its asyncio task's: while the task awaits, the profile shows what it
awaits (`build_report` → `asyncio.sleep`, with the line of the `await`)
instead of the idle event loop, so requests sharing a loop each get their
samples. A moment the profiler can't tell between two traces on one thread
is counted as dropped.

Every profile counts garbage collections, with how long each one stopped
the trace and when. A profile counts memory too when asked: an
`X-Tracepanel-Profile: memory` header, a `tracepanel_profile=memory` cookie,
or `profile_memory = true` for every profiled trace. Tasks a request asking
for memory queues count theirs too. Memory is counted with Python's
tracemalloc: the peak above the level at the trace's start, what is still
allocated at its end (`net`), and the 50 sites that held the most at the
peak, each the project's line that allocated or called the library that did.
tracemalloc starts with the first trace that asks and stops once none has
run for a while. It slows down every allocation the whole process makes
meanwhile: a short request can take several times longer, and the requests
running beside it slow down too. An app that runs tracemalloc itself keeps
it as it is, and its peak is not reset. tracemalloc counts the whole
process: a trace that ran beside others says its numbers are shared. A trace
that isn't profiled pays for none of this.

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

## Command line

| Command | What it does |
| --- | --- |
| `tracepanel run <command>` | runs a command with Tracepanel on |
| `tracepanel dev` | runs the agent in the foreground, with the console on http://127.0.0.1:8765/ |
| `tracepanel config` | prints the settings in effect and where each comes from |
| `tracepanel clear` | deletes every stored trace, log, query, email and issue |
| `tracepanel mcp` | gives an AI coding agent your traces over MCP |

## Supported versions

| Python | Django |
| --- | --- |
| 3.11 | 5.2 |
| 3.12, 3.13, 3.14 | 5.2, 6.0, 6.1 |
| 3.15 | 6.1 |

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

Licensed under the [MIT License](LICENSE).
