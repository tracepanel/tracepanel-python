# Changelog

## 0.3.4 (2026-10-19)

### Fixed

- EXPLAIN in the console runs a query that has no parameters, such as
  `SELECT name FROM sqlite_master WHERE type = 'table'`. It answered that the
  query's parameters were not captured, since there were none to keep. A
  query with placeholders still needs its captured parameters.

## 0.3.3 (2026-10-17)

### Fixed

- FastAPI: a SQLAlchemy transaction that begins in a sync dependency now
  holds what the endpoint does until it commits. FastAPI runs each sync
  dependency and endpoint in a worker thread, in a copy of the request's
  context. The transaction's span stayed current only in the dependency's
  copy, so the endpoint's queries and HTTP calls were drawn beside it. Calls
  made before the commit were not reported as made inside a transaction.
- FastAPI: the routes of a router added with `include_router()` run in a
  `view` span, as routes declared on the app do. FastAPI 0.142 includes
  routers lazily, and their routes had none.
- Falcon: an exception the app answers with an error handler of its own
  (`app.add_error_handler()`) is no longer recorded as an error. Only one
  that Falcon's default handler turns into a 500 is.
- The console reaches the local agent without the system's or the
  environment's proxy settings. On macOS, looking the system's proxy up
  crashed a forked gunicorn worker.

## 0.3.2 (2026-10-17)

### Changed

- Requires any `tracepanel-agent` 0.3 from 0.3.1 on, rather than exactly its
  own version. A minor version is still released together with the agent's.
  A patch of either now comes out on its own.

### Fixed

- A SQLAlchemy connection in `AUTOCOMMIT`, set on the engine or the
  connection, no longer shows as a transaction. SQLAlchemy begins one there
  too, but nothing is held open. Statements on it, and calls between them,
  were drawn inside a transaction that never existed, and could be reported
  as a long transaction or as work done before commit.

## 0.3.1 (2026-10-16)

Needs `tracepanel-agent` 0.3.1 (installed with it).

### Changed

- `install(app, **settings)` (FastAPI, Starlette and any ASGI app, Flask,
  Falcon, WSGI) and `conf.configure()` take typed keyword arguments
  (`Settings`). A type checker and the editor know every setting's name and
  type.

### Fixed

- The widget is injected again behind a Starlette `BaseHTTPMiddleware`. It
  sends every response in several messages, so each looked streamed and went
  out without the widget, `Tracepanel-Url` and `Server-Timing`. A response
  with a `Content-Length` (up to 8 MB) is now held whole. An HTML page that
  is streamed says so once in the terminal.

- A query whose stack holds no project frame, such as one a response schema's
  resolver runs under Django Ninja or FastAPI, is no longer placed in the
  environment's scripts (`.venv/bin/uvicorn:10 in <module>`): they count as
  installed code, as site-packages does.

## 0.3.0 (2026-10-15)

Needs `tracepanel-agent` 0.3.0 (installed with it): its console finds logs by
the spans they were written in, follows calls to another project's agent,
and keeps issues whole when old traces go. The store starts empty after the
upgrade.

### Changed

- A trace keeps up to 1,000 log records, not 200: the limit every SDK and
  the agent now share. The rest are counted (`tracepanel.dropped_logs`).
- The app's console answers what it adds to the agent's
  (`api/capabilities`): EXPLAIN where it has the database, switching users
  in Django with its auth and sessions, in DEBUG. The console shows those
  only then, and asks nothing of an app that can't answer.

## 0.2.0 (2026-10-14)

Needs `tracepanel-agent` 0.2.0 (installed with it): its Pages section lists
what users do in a browser, and apps in many more languages and frameworks
are labelled.

### Added

- The app's console (`/__tracepanel__/`) passes a page's traces recorded in
  the browser on to the agent (`POST api/ingest`), for Django, ASGI and WSGI
  apps: an SPA's dev server (tracepanel-js's Vite plugin) sends them there,
  and the console lists them under Pages.

### Security

- The app's console takes writes to the agent (a page's traces, clearing
  the store) from this machine only, or from Django's `INTERNAL_IPS`. With
  DEBUG on a host others can reach, they could fill or clear the store. A
  write over 8 MB is answered 413 without being read.

## 0.1.1 (2026-10-13)

Needs `tracepanel-agent` 0.1.1 (installed with it): its Services map fits the
width it has, and its Share dialog and Issues page say what they do now.

### Fixed

- A script or a worker under `tracepanel run` with no web framework traced
  only Celery, Dramatiq, RQ, SQLAlchemy, Strawberry and logging. Its database
  drivers, HTTP clients, Redis and MongoDB weren't recorded, so a job wrapped
  in `tracepanel.trace()` showed its own steps and none of its calls. Every
  library is now traced in such a process too, once it's imported.

## 0.1.0 (2026-10-13)

Needs `tracepanel-agent` 0.1.0 (installed with it): the agent that keeps a
MongoDB query's values apart from its shape, finds slow datastore and gRPC
calls with each system's hints, lists the gRPC methods an app serves, and
shares a trace as one HTML file. Its store format changed: the traces kept by
an earlier agent aren't carried over.

### Added

- MongoDB commands as `mongodb` spans, from pymongo (`MongoClient`,
  `AsyncMongoClient`, and Motor on top of them), with nothing to install or
  configure beyond this package: through pymongo's own command monitoring, for
  every client created after the app imports pymongo. Each span has the
  command's shape (`{"find": "users", "filter": {"email": "?"}}`), its values
  apart as SQL's (`db.params`, kept on this machine and only with
  `capture_sql_params`), the database, the server and the line of code. The
  agent finds a `find` per item as an N+1 and the same `find` with the same
  values as a duplicate (needs an agent that reads `db.params_hash` on
  datastore calls). `mongodb_enabled` turns it off. An app that also runs
  OpenTelemetry's pymongo instrumentation records each command once.
- Calls between apps link their traces, by the W3C trace context: an
  outgoing HTTP call (urllib3, requests, httpx, aiohttp) carries a
  `traceparent` header unless the request has its own, and a request that
  comes with one (Django, ASGI, WSGI) continues its W3C trace. The agent then
  links the called app's trace to the call, whichever app recorded it, this
  SDK or OpenTelemetry. Needs an agent that reads the `w3c.*` attributes
  (after 0.0.1a2).
- SQL, an HTTP call, a Redis command or a Django cache call made
  synchronously on an event loop's thread (a sync client or driver in an
  async view or endpoint) is marked `code.blocks_event_loop`: the agent finds
  it as a `blocking-call` issue. Async clients, and sync calls in a thread
  (`sync_to_async`, `asyncio.to_thread`, a sync view), aren't marked.
- An `sql` span says how many rows the query returned
  (`db.response.returned_rows`): counted as Django or SQLAlchemy (the ORM
  too, sync or async) fetch them, on every database. A driver used directly
  gives its `rowcount` where it knows it (psycopg, the MySQL drivers), asyncpg
  its results, and sqlite3 and aiosqlite count what is fetched. The agent
  finds a SELECT without a LIMIT that read many rows as a `large-result`
  issue.
- asyncpg's prepared statements (`fetch`, `fetchrow`, `fetchval`,
  `fetchmany`, `executemany`) and server-side cursors are recorded: a cursor
  as one statement, with every row read from it, however many chunks. So
  are the rows read from psycopg's server-side (named) cursors.

### Changed

- A library is patched once the app imports it, not at startup: Tracepanel no
  longer loads every library it knows that happens to be installed. In an
  environment with all of them, Django's startup took 480 ms and 100 MiB
  more with Tracepanel, now 16 ms and 2 MiB, and the smaller heap makes the
  garbage collector's work lighter on every request: Tracepanel added 2 ms to
  a request with 17 queries, now 0.6 ms, and 26 ms to loading 20,000 model
  objects, now 0.8 ms.
- Loading model instances costs less: Django's `pre_init` and `post_init`,
  sent twice for every object a query loads, no longer pass through the
  signal tracing at all (about 0.6 µs an object before).
- `scripts/bench.py rows`: reading 20,000 rows by each way the SDK counts
  them, with and without Tracepanel.

### Fixed

- `importlib.resources` on a package Tracepanel waited for under
  `tracepanel run` (Celery, SQLAlchemy): its loader now answers for the
  package's own.
- `async with connection.execute(...)` on aiosqlite raised a `TypeError`
  while Tracepanel recorded it: the traced call can be awaited or entered
  again, as aiosqlite's own.

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
