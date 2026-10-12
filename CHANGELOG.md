# Changelog

## Unreleased

### Added

- SQL from sqlite3 and psycopg (3) used directly, without an ORM: an app that
  talks to its database through the driver gets the same `sql` spans as Django's
  ORM and SQLAlchemy, with N+1, duplicate and slow query issues. A statement an
  ORM runs through the driver is still recorded once, by the ORM. The
  `dbapi_enabled` setting turns it off.

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
