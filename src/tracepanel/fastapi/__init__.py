"""FastAPI: an ASGI app, traced by tracepanel.asgi (this module keeps the name FastAPI users look for).

`tracepanel run uvicorn app.main:app` needs no code. In code, `tracepanel.fastapi.install(app)`, on only with
`FastAPI(debug=True)` or TRACEPANEL_ENABLED=1. See tracepanel.asgi for what is traced.
"""

from typing import TYPE_CHECKING, Unpack

from tracepanel import asgi
from tracepanel.core.conf import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI


def install(app: "FastAPI", **settings: Unpack[Settings]) -> None:
    asgi.install(app, **settings)
