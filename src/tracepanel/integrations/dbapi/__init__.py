"""Database drivers used directly, without an ORM: every statement as an `sql` span.

The third database layer on tracepanel.core.sql (Django's ORM and SQLAlchemy are the others): the statement, its
parameters, its place in project code and the N+1 / duplicate analysis are the same. An app that talks to its
database through the driver (FastAPI or Flask with raw SQL, Peewee, a script) gets its queries traced like an ORM's.

- SQLite: sqlite3, aiosqlite.
- PostgreSQL: psycopg (3), psycopg2, asyncpg.
- MySQL and MariaDB: PyMySQL, mysqlclient, MySQL Connector/Python, aiomysql, asyncmy.
- Oracle: python-oracledb.
- SQL Server: pyodbc, pymssql.

An ORM runs its statements through the same drivers, and records them itself. What decides is who called the driver
(tracepanel.core.callers): the first frame above the call that is neither the driver's own code nor this package's.
When that is Django's database layer, SQLAlchemy or tracepanel itself (EXPLAIN), the statement is theirs and the
driver's wrapper leaves it alone; the app, or a library on top of the driver, gets it recorded here.
"""

from collections.abc import Callable

from tracepanel.core import explain as plans
from tracepanel.integrations.dbapi._core import explain, known

__all__ = ["explain", "explain_or", "install", "known"]


def explain_or(fallback: Callable[..., plans.Plan]) -> Callable[..., plans.Plan]:
    """EXPLAIN for a statement a driver recorded, else for one of the framework's ORM (`fallback`)."""

    def run(statement: str, params: object, alias: str, *, analyze: bool = False) -> plans.Plan:
        if known(alias):
            return explain(statement, params, alias, analyze=analyze)  # type: ignore[arg-type]
        return fallback(statement, params, alias, analyze=analyze)

    return run


def install() -> None:
    """Wrap the drivers that are installed (sqlite3 always: it is the standard library)."""
    # Here rather than at the top: the package is imported for EXPLAIN before the app imports a driver, and the sqlite
    # module imports sqlite3, whose import hook installs this package (while it is still importing, without install()).
    from tracepanel.integrations.dbapi import mssql, mysql, oracle, postgres, sqlite  # noqa: PLC0415

    for family in (sqlite, postgres, mysql, oracle, mssql):
        family.install()
