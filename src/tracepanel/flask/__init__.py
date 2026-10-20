"""Flask: a WSGI app traced by tracepanel.wsgi, plus what only Flask knows.

`tracepanel run flask --app app run` needs no code: the app is set up as it is created. In code,
`tracepanel.flask.install(app)` does the same, on only in debug mode (`flask run --debug`, `app.run(debug=True)`) or
with TRACEPANEL_ENABLED=1.

tracepanel.wsgi makes requests traces (status, the widget's headers and script, the console under `/__tracepanel__/`);
this module names them after the URL rule, runs the view in a `view` span, records errors Flask turns into a 500,
and puts the trace's headers on Werkzeug's debugger page. The framework-free integrations turn on too: Jinja (each
render a `template` span), SQLAlchemy (Flask-SQLAlchemy too), Celery, logging.
Settings come from `app.config["TRACEPANEL"]` (the same names as Django's TRACEPANEL dict) and install()'s keyword
arguments; TRACEPANEL_* variables override them. The session is recorded as the view left it, and Flask-Login's user
when it loaded one; there is no user switching: Flask has no user model of its own.
"""

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Unpack, cast

from tracepanel import _setup
from tracepanel.core import conf, errors
from tracepanel.core import request as details
from tracepanel.core.conf import Settings
from tracepanel.core.context import current_trace, start_span
from tracepanel.core.model import Attrs, Trace
from tracepanel.core.patching import patch, patched
from tracepanel.wsgi import TRACE_KEY, URL_KEY, TracepanelMiddleware

if TYPE_CHECKING:
    from wsgiref.types import StartResponse as WSGIStartResponse

    from flask import Flask

MARKER = "_tracepanel"
StartResponse = Callable[..., Any]


def _debugger() -> None:
    """Werkzeug's debugger page carries the failed request's trace headers, as Flask's own 500 does."""
    try:
        from werkzeug.debug import DebuggedApplication  # noqa: PLC0415 (Flask's own dependency)
    except ImportError:
        return
    if patched(DebuggedApplication, "__call__"):
        return
    original = DebuggedApplication.__call__

    def call(self: DebuggedApplication, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        def respond(status: str, headers: list[tuple[str, str]], exc_info: object = None) -> object:
            trace_id = environ.get(TRACE_KEY)
            if trace_id:
                headers = [
                    *headers,
                    ("Tracepanel-Id", trace_id),
                    ("Tracepanel-Url", f"{environ[URL_KEY]}#/t/{trace_id}"),
                ]
            return start_response(status, headers, exc_info)

        return original(self, environ, cast("WSGIStartResponse", respond))

    patch(DebuggedApplication, "__call__", call)


def _view_name(app: "Flask", endpoint: str | None) -> str | None:
    view = app.view_functions.get(endpoint) if endpoint else None
    if view is None:
        return None
    target = getattr(view, "view_class", None) or view
    return f"{getattr(target, '__module__', '')}.{getattr(target, '__qualname__', endpoint)}"


def _dispatch(app: "Flask") -> Callable[[], Any]:
    """The view inside a `view` span, named after its function; the trace after its URL rule."""
    original = app.dispatch_request

    def dispatch_request() -> object:
        from flask import request  # noqa: PLC0415 (Flask is imported by the app that installs tracepanel)

        trace = current_trace()
        rule = request.url_rule
        if trace is None or rule is None:
            # No trace, or a 404/405 that Flask raises before any view.
            return original()
        trace.attrs["http.route"] = rule.rule
        trace.name = f"{request.method} {rule.rule}"
        name = _view_name(app, request.endpoint) or str(request.endpoint)
        trace.attrs["code.view"] = name
        with start_span("view", name, {"code.function": name}):
            result = original()
        trace.attrs.update(_who(trace))
        operation = trace.attrs.get("graphql.operation")
        if isinstance(operation, str):
            trace.name = f"GraphQL {operation}"
        return result

    return dispatch_request


def _who(trace: Trace) -> Attrs:
    """The session as the view left it, and the user Flask-Login loaded for it, if it did."""
    from flask import g, session  # noqa: PLC0415

    attrs: Attrs = {}
    if conf.get_config().capture_request:
        # Already decoded from its cookie: reading it costs nothing, and items() doesn't mark it accessed.
        attrs.update(details.session(session.items()))
    # Flask-Login keeps the user it loaded on g; reading current_user here would load one the view never asked for.
    loaded = g.get("_login_user")
    if loaded is not None and "user.id" not in trace.attrs:
        attrs.update(details.user(loaded.get_id(), _username(loaded), authenticated=bool(loaded.is_authenticated)))
    return attrs


def _username(user: object) -> object:
    """What a Flask-Login user is called: its username, else its email; Flask-Login asks for neither."""
    return getattr(user, "username", None) or getattr(user, "email", None)


def _on_exception(sender: object, exception: BaseException, **extra: object) -> None:
    del sender, extra
    trace = current_trace()
    if trace is not None and isinstance(exception, Exception):
        errors.capture(exception, handled=False)
        trace.status = "error"


def install(app: "Flask", **settings: Unpack[Settings]) -> None:
    from flask import got_request_exception  # noqa: PLC0415

    if getattr(app, MARKER, False):
        return
    setattr(app, MARKER, True)

    def source() -> dict[str, object]:
        # Like Django's DEBUG: on in development only (TRACEPANEL_ENABLED, set by `tracepanel run`, wins). Read on each
        # load, so `flask run --debug` (which sets debug after the app is created) counts.
        return {"enabled": bool(app.debug), **app.config.get("TRACEPANEL", {}), **settings}

    # Installed even while off: app.run(debug=True) turns debug mode on after this; the middleware checks per request.
    _setup.enable(source)
    # Flask's documented way to wrap it; the middleware reads the settings again on the first request, as
    # `flask run --debug` turns debug on after the app is created.
    app.wsgi_app = TracepanelMiddleware(app.wsgi_app)  # type: ignore[method-assign]
    app.dispatch_request = _dispatch(app)  # type: ignore[method-assign]
    got_request_exception.connect(_on_exception, app, weak=False)
    _debugger()
    _setup.started("Flask")
