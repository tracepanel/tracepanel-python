# Tracepanel

**See what your Django app is really doing.**

`pip install tracepanel` is all you need in development: Tracepanel collects requests, SQL, cache calls, logs and errors, and the Tracepanel agent it installs and starts finds N+1 queries, duplicates and slow SQL, keeps the traces in `.tracepanel/` and shows them in a widget on your pages and at `/__tracepanel__/`.

```python
# settings.py
if DEBUG:
    INSTALLED_APPS += ["tracepanel.django"]
```

## Instrument your own code

Requests, Celery tasks and management commands are traced on their own. To see your own steps in them, or to trace work that runs elsewhere (a script, a consumer, a scheduled job):

```python
import tracepanel

with tracepanel.span("pricing.calculate", cart_id=cart.id) as span:  # a span in the current trace
    total = calculate(cart)
    span.set("total", total)


@tracepanel.traced  # sync and async; named after the function, or @tracepanel.traced("name")
def rebuild_index(): ...


with tracepanel.trace("sync-prices"):  # a trace of its own (inside another trace it is just a span)
    sync_prices()

tracepanel.set_attr("tenant", tenant.slug, on_trace=True)  # searchable with the trace
tracepanel.event("cache warmed", keys=120)  # a point on the timeline
```

Keyword attributes go under `app.` (`cart_id` → `app.cart_id`); pass `attrs={"shop.region": "eu"}` to choose the namespace. Values are scalars or lists of scalars, anything else is stored as a masked `repr()`. SQL, logs and errors inside a span attach to it, an exception marks it as failed. Outside a trace and outside `DEBUG` every call does nothing, so instrumentation can stay in production code.

## Services

Every outgoing call records what it talks to, with OpenTelemetry attribute names: `db.system`, `db.namespace`, `cache.system`, `mail.system`, `messaging.system`, `server.address`, `server.port` (credentials are never kept). Each trace carries `service.name` (the `service` setting, the project directory's name by default) and `service.role` (web, worker, command). The console's Services page lists them with who calls them.

## Supported versions

| Python | Django |
| --- | --- |
| 3.11 | 5.2 |
| 3.12, 3.13, 3.14 | 5.2, 6.0, 6.1 |

## Repository

| Path | Contents |
| --- | --- |
| `tracepanel/` | The package |
| `tests/` | pytest suite; every trace is validated against the agent's trace schema, and the agent is checked through the console's API |
| `scripts/check_dist.py` | Fails a build whose wheel or sdist holds the agent's files (the UI, the store) |

Package layout. Django is the framework Tracepanel is built for; ASGI apps (`tracepanel.asgi`: Starlette, FastAPI, any other) and WSGI apps (`tracepanel.wsgi`: Flask, Falcon, any other) are covered too. Only these packages know their framework: the core, the transport and the console are framework-neutral (import-linter checks this in `poe lint`). Imports use full paths.

Tracepanel records and sends; the Tracepanel agent (a dependency, started on the first trace and gone after 30 minutes unused) keeps the traces, finds their issues and serves the console, which `/__tracepanel__/` forwards to. Without a web server to host it (a Celery worker, management commands), each task run and command prints one line with a link to its trace in the agent's console; `tracepanel dev` runs the agent in the foreground on http://127.0.0.1:8765/.

| Module | Contents |
| --- | --- |
| `tracepanel/__init__.py`, `api.py` | Public API: `span`, `traced`, `trace`, `set_attr`, `event` |
| `tracepanel/core/` | Trace model, context, configuration, frames, errors, SQL spans (and their fingerprints) and EXPLAIN plans, service attributes; `registry` (every integration: its library, its setting, where its code is) and `patching` (every monkeypatch: once, remembered, undoable) |
| `tracepanel/storage/` | The background writer and the agent: finding it, starting it, sending to it |
| `tracepanel/console/` | The console under the prefix (forwarded to the agent, as a plain Request → Response), widget markup, the terminal line after tasks and commands |
| `tracepanel/django/` | The Django integration: `INSTALLED_APPS` app, middleware, collectors (ORM via execute wrappers, cache, mail, signals, views and templates, transactions, commands, auth), console adapter, user switching, EXPLAIN runner, Channels, Django Tasks, Django Ninja, Django REST framework |
| `tracepanel/asgi/` | Any ASGI app: `tracepanel.asgi.install(app)` and the middleware (requests, the widget, the console under the prefix); Starlette and FastAPI add the route's template and endpoints as view spans, mounts included |
| `tracepanel/fastapi/` | `tracepanel.fastapi.install(app)`: tracepanel.asgi under the name FastAPI users look for |
| `tracepanel/wsgi/` | Any WSGI app: `tracepanel.wsgi.install(app)` and the middleware (requests named after their path, the widget's headers and script on complete responses, the console under the prefix) |
| `tracepanel/flask/` | Flask on tracepanel.wsgi: the URL rule names the request, the view and each Jinja render are spans, errors Flask turns into a 500, the trace's headers on Werkzeug's debugger page |
| `tracepanel/falcon/` | Falcon (WSGI) on tracepanel.wsgi, in place: the URI template names the request, the responder Falcon picked is the view span, errors Falcon turns into a 500 |
| `tracepanel/integrations/` | Framework-free libraries, enabled when installed: `logging`, `celery`, `dramatiq`, `rq`, `strawberry`, `sqlalchemy` |
| `tracepanel/_setup.py` | The steps every framework's install takes: `enable(settings)`, then its own middleware and patches, then `started(name)` (integrations, EXPLAIN, terminal lines) |
| `tracepanel/_cli.py`, `_agent.py`, `_compat.py` | CLI and other internals |

Tests mirror the source (`tests/core/`, `tests/storage/`, `tests/console/`, `tests/django/`, `tests/integrations/`); `tests/project/` is the Django project they run against.

Related private repositories: tracepanel-ui (widget and local UI), tracepanel-examples (test projects). Nothing is published to PyPI or npm.

## Development

```bash
uv sync --all-groups   # builds the agent from a ../tracepanel-agent checkout until it is on the package index
uv run poe test    # pytest on SQLite; TRACEPANEL_TEST_DB=postgres for Postgres (PG* variables)
uv run poe fmt     # ruff check --fix + ruff format
uv run poe lint    # ruff (ALL) + ruff format --check + pyrefly (strict)
```

`uv run poe build` runs `uv build` and checks that the wheel and sdist hold the SDK only.

## Contract with tracepanel-ui

The widget and the UI depend on:

- response headers `Tracepanel-Id`, `Tracepanel-Url`, `Server-Timing` (`db`, `app`, `total`, `tracepanel-issues;desc="pending"`: the agent finds them, the widget asks the console);
- `data-tracepanel-*` attributes on the injected `<script>`: `api`, `id`, `method`, `status`, `total`, `db`, `queries`, `issues`;
- the console under `/__tracepanel__/`: the agent's, forwarded, but for `api/explain` and `api/users*`, which the app serves.

Changing any of these needs a matching change in tracepanel-ui.
