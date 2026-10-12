# Changelog

## Unreleased

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
  Redis as a datastore, with the command and its key. Django's cache on Redis
  stays a cache, with hits and misses.
  A pipeline is one span. Django's cache and the task queues' own Redis calls
  are left to them. The `redis_enabled` setting turns it off.

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
