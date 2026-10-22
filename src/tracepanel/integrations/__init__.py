"""Integrations: what tracepanel knows about the libraries a project uses, each turned on only when installed.

The frameworks are tracepanel.django (the one tracepanel is built for), tracepanel.asgi and tracepanel.wsgi (with
FastAPI, Flask and Falcon on top); these don't need one: `logging` (stdlib), `celery`, `dramatiq`, `rq`, `huey`,
`strawberry`, `sqlalchemy`. Which exist, and when each is installed, is tracepanel.core.registry.
"""
