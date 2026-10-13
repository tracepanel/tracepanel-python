# Changelog

## Unreleased

### Added

- Calls between apps link their traces, by the W3C trace context: an
  outgoing HTTP call (urllib3, requests, httpx, aiohttp) carries a
  `traceparent` header unless the request has its own, and a request that
  comes with one (Django, ASGI, WSGI) continues its W3C trace. The agent then
  links the called app's trace to the call, whichever app recorded it, this
  SDK or OpenTelemetry. Needs an agent that reads the `w3c.*` attributes
  (after 0.0.1a2).

## 0.0.1a2 (2026-10-12)

Needs `tracepanel-agent` 0.0.1a2 (installed with it): the agent that knows the
`redis` spans and shows the new Redis, Cache and HTTP calls pages.

### Added

- SQL from database drivers used directly, without an ORM: sqlite3, aiosqlite,
  psycopg (3), psycopg2, asyncpg, PyMySQL, mysqlclient, MySQL Connector/Python,
  aiomysql and python-oracledb. An app that talks to its database through the
  driver gets the same `sql` spans as Django's ORM and SQLAlchemy, with N+1,
  duplicate and slow query issues, and EXPLAIN in the console (SQLite files,
  PostgreSQL and MySQL). A statement an ORM runs through the driver is still
  recorded once, by the ORM. The `dbapi_enabled` setting turns it off.
- Outgoing HTTP calls as `http.client` spans: urllib3 and what is built on it
  (requests, boto3), httpx, httpx2 and aiohttp. Credentials and the values of
  sensitive query parameters stay out of the recorded URL. A status of 400 or
  more marks the call as an error. The `http_clients_enabled` setting turns it
  off. Calls slower than `slow_http_ms` (500 ms by default) are reported as
  slow.
- Redis commands as `redis` spans, from redis-py and valkey-py, sync and async:
  Redis as a datastore, with the command and its key. A pipeline is one span.
  The `redis_enabled` setting turns it off.
- A cache operation is a span named after its operation and key
  (`get user:1`), open while the backend works: a cache on Redis has its
  commands inside, with its hit or miss on the cache span. A task enqueue has
  its broker's commands inside the same way.
- Repeated Redis commands and cache calls are reported only when they cost
  `n_plus_one_min_ms` (5 ms by default) together: an in-memory store answers
  in a fraction of a millisecond.

### Changed

- Cache spans are named after their operation and key (`get user:1`), not
  `cache.get`, and carry `cache.library` and `code.origin`.
- Redis commands a library sends for the app (Django's cache, Celery, RQ,
  Dramatiq, Channels) are recorded, under that library's span, instead of left
  out.

## 0.0.1a1 (2026-10-12)

The first release of Tracepanel for Python. It is an alpha: settings and what
the package records can still change between versions.

### Added

- Tracing for Django, FastAPI, Starlette, Flask and Falcon: every request with
  its SQL, cache calls, templates, signals, emails, logs and errors.
- Celery, Dramatiq, RQ, Django Tasks, SQLAlchemy, Strawberry, Django REST
  framework, Django Ninja, Channels, htmx and OpenTelemetry, traced when they
  are installed.
- Log lines written with `logging`, each attached to the request, task or
  command that wrote it, with the fields passed in `extra=`. Tracepanel adds no
  handler and leaves the logging configuration as it is.
- `tracepanel run` turns Tracepanel on for any command, with no change to the
  project.
- A badge and a panel on your pages, the console under `/__tracepanel__/`, and
  a line in the terminal after each task and command.
- `span()`, `@traced`, `trace()`, `set_attr()` and `event()` to trace your own
  code.
- Settings in `tracepanel.toml`, `[tool.tracepanel]` in `pyproject.toml`,
  Django's `TRACEPANEL` dict or `TRACEPANEL_<NAME>` variables. Several apps can
  share one agent, each under its own `service` name.
- The package starts the agent on the first trace, and again if it stopped.
- `tracepanel config`, `tracepanel dev`, `tracepanel clear` and
  `tracepanel mcp`.
- Python 3.11 to 3.15, Django 5.2, 6.0 and 6.1.
