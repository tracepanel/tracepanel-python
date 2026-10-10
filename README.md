# Tracepanel

**See what your app does on every request.** Tracepanel is a debug toolbar for
Python web apps. It traces every request, query, task and error on your
machine, finds the N+1 queries and the duplicate and slow SQL, and names the
line of code to fix. It shows all of it in a panel on your pages and in a
console.

It runs while you develop. Nothing leaves your machine.

## Install

```bash
pip install tracepanel
```

Tracepanel turns on in development only: with `DEBUG`, in a debug server, or
under `tracepanel run`. Add it for your framework:

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

Open any page of your app. A badge in the corner opens the panel, and the
console is at `/__tracepanel__/`. Celery, Dramatiq, RQ, SQLAlchemy, Strawberry,
Django REST framework, Django Ninja, Channels and htmx are traced with no more
setup.

For Django, `pip install django-tracepanel` installs the same package, and
[its documentation](https://github.com/tracepanel/django-tracepanel) covers
Django in depth.

## Run without changing your code

`tracepanel run` turns Tracepanel on for one command and adds nothing to the
project:

```bash
tracepanel run python manage.py runserver   # Django
tracepanel run uvicorn app.main:app         # FastAPI, Starlette
tracepanel run flask --app app run          # Flask
tracepanel run gunicorn app:app             # Falcon
tracepanel run celery -A app worker         # a worker
```

## Configure

Tracepanel works with no settings. To change one, set it in any of these
places. A later one wins:

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

`tracepanel config` prints every setting, its value and where it was set.

## Trace your own code

Requests, tasks and management commands are traced already. To see your own
steps inside them, or to trace work that runs elsewhere, such as a script or a
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

`span()` adds a step to the current trace, `@traced` does the same for a
function, and `trace()` starts a trace of its own. Keyword attributes go under
`app.`: `source` is stored as `app.source`. SQL, logs and errors inside a span
belong to it. Outside development every call does nothing, so the
instrumentation can stay in your code.

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

## Links

- [tracepanel](https://github.com/tracepanel/tracepanel): what Tracepanel finds,
  how it works, privacy, and the agent this package installs
- [django-tracepanel](https://github.com/tracepanel/django-tracepanel): the
  documentation for Django
- [CHANGELOG.md](CHANGELOG.md): what changed in each version
- [CONTRIBUTING.md](CONTRIBUTING.md): developing this package

## License

[MIT](LICENSE). © DevRealm OÜ.
