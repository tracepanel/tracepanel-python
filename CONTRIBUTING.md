# Contributing

This repository is the `tracepanel` Python package. It records traces and sends
them to the Tracepanel agent, which keeps them, finds their issues and serves
the console. The agent and its console live in
[tracepanel](https://github.com/tracepanel/tracepanel).

## Set up

Check out tracepanel next to this repository, then:

```bash
uv sync --all-groups   # builds the agent from ../tracepanel
uv run poe test        # pytest on SQLite
uv run poe lint        # ruff, pyrefly (strict), import-linter, the docs
uv run poe fmt         # ruff check --fix and ruff format
```

`TRACEPANEL_TEST_DB=postgres` runs the tests on PostgreSQL, with the usual
`PG*` variables. `uv sync --no-sources` installs the published agent in place
of the checkout.

The database drivers' tests (`tests/integrations/dbapi/`) need their servers.
Without them they are skipped. Start the servers in Docker:

```bash
docker run -d --name pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=tracepanel -p 5432:5432 postgres:17
docker run -d --name mysql -e MYSQL_ROOT_PASSWORD=mysql -e MYSQL_DATABASE=tracepanel -p 3306:3306 mysql:8.4
docker run -d --name oracle -e ORACLE_PASSWORD=oracle -e APP_USER=tracepanel \
  -e APP_USER_PASSWORD=tracepanel -p 1521:1521 gvenzl/oracle-free:23-slim-faststart
TRACEPANEL_TEST_DB=postgres TRACEPANEL_TEST_MYSQL=1 TRACEPANEL_TEST_ORACLE=1 uv run poe test
```

mysqlclient builds against the MySQL client library: `brew install mysql-client
pkg-config` on macOS, `apt install default-libmysqlclient-dev pkg-config` on
Debian and Ubuntu.

`uv run poe build` runs `uv build` and checks that the wheel and the sdist hold
the package only, none of the agent's files.

## Measure what it costs

`tests/benchmarks/` holds what Tracepanel costs the app: requests through
Django, reading many rows, recording statements, encoding a trace. CodSpeed
runs them on every pull request, counts the instructions each one takes and
comments when one costs more. To run them yourself:

```bash
uv run pytest tests/benchmarks --codspeed
```

They run as plain tests too, once each, in `poe test`. `uv run poe bench`
compares a process with Tracepanel and one without: the time per request, the
time to read 20,000 rows each way, and the memory over many requests.

## The package

Django is the framework Tracepanel started with. `tracepanel.asgi` covers ASGI
apps such as Starlette and FastAPI, and `tracepanel.wsgi` covers WSGI apps such
as Flask and Falcon. Only the framework packages import their framework. The
core, the storage and the console stay framework-neutral, and import-linter
enforces that. Imports use full paths.

| Module | Contents |
| --- | --- |
| `tracepanel/__init__.py`, `api.py` | Public API: `span`, `traced`, `trace`, `set_attr`, `event` |
| `tracepanel/core/` | Trace model, context, configuration, frames, errors, SQL spans and their fingerprints, EXPLAIN plans, service attributes; `registry` (every integration) and `patching` (every monkeypatch: once, remembered, undoable) |
| `tracepanel/storage/` | The background writer and the agent: finding it, starting it, sending to it |
| `tracepanel/console/` | The console under the prefix (forwarded to the agent), the widget's markup, the terminal line after tasks and commands |
| `tracepanel/django/` | The Django app, middleware, collectors (ORM, cache, mail, signals, views and templates, transactions, commands, auth), user switching, EXPLAIN, Channels, Django Tasks, Django Ninja, Django REST framework |
| `tracepanel/asgi/` | `tracepanel.asgi.install(app)` and the ASGI middleware. Starlette and FastAPI add the route and the endpoint |
| `tracepanel/fastapi/` | `tracepanel.fastapi.install(app)`: `tracepanel.asgi` under the name FastAPI users look for |
| `tracepanel/wsgi/` | `tracepanel.wsgi.install(app)` and the WSGI middleware |
| `tracepanel/flask/` | Flask on `tracepanel.wsgi`: the URL rule, the view, Jinja renders, errors, Werkzeug's debugger page |
| `tracepanel/falcon/` | Falcon on `tracepanel.wsgi`: the URI template, the responder, errors |
| `tracepanel/integrations/` | Libraries traced when installed: `logging`, `celery`, `dramatiq`, `rq`, `strawberry`, `sqlalchemy`, `htmx`, the database drivers (`dbapi`), OpenTelemetry |
| `tracepanel/_setup.py` | The steps every framework's install takes |
| `tracepanel/_cli.py`, `_run.py`, `_agent.py` | The command line, `tracepanel run`, the agent's binary |

The tests mirror the package. `tests/project/` is the Django project they run
against. The tests check every trace they record against the agent's trace
schema, and check the agent through the console's API.

## The contract with the console

The widget and the console read what this package puts on a response:

- the headers `Tracepanel-Id`, `Tracepanel-Url` and `Server-Timing` with
  `db`, `app`, `total` and `tracepanel-issues;desc="pending"`. The last one
  means the agent is still finding the issues, and the widget asks the console
  for them.
- the `data-tracepanel-*` attributes of the injected `<script>`: `api`, `id`,
  `method`, `status`, `total`, `db`, `queries`, `issues`
- the console under `/__tracepanel__/`, which the app forwards to the agent,
  except `api/explain` and `api/users*`, which the app serves itself

A change to any of these needs the same change in tracepanel's `ui/`.

## Write the documentation

Read `.claude/skills/docs/SKILL.md` for the voice and the rules. `poe lint`
checks the pages.

## Release

Read `.claude/skills/release/SKILL.md`.
