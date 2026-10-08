"""Performance and memory checks for tracepanel in a Django process.

    uv run python scripts/bench.py              # everything
    uv run python scripts/bench.py overhead     # time per request, with and without tracepanel
    uv run python scripts/bench.py memory       # many requests: does memory or internal state grow?
    uv run python scripts/bench.py store        # console API timings on a full store, store file size

Each mode runs in its own process (tracepanel patches Django at startup), on the test project (tests/) with an
in-memory database and a throwaway .tracepanel/ directory.
"""

import argparse
import asyncio
import gc
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
VIEWS = {
    "ignored (/health/)": "/health/",
    "plain": "/",
    "sql x17 (N+1)": "/n-plus-one/",
    "template N+1": "/users-page/",
    "duplicates": "/duplicates/",
    "logged exception": "/logs/",
}
ASYNC_VIEW = "/async/n-plus-one/"


def setup(*, tracepanel: bool) -> None:
    sys.path.insert(0, str(ROOT))
    os.environ["DJANGO_SETTINGS_MODULE"] = "tests.project.settings"
    from tests.project import settings  # noqa: PLC0415

    if not tracepanel:
        settings.INSTALLED_APPS.remove("tracepanel.django")
    import django  # noqa: PLC0415

    django.setup()
    from django.contrib.auth.models import Group, User  # noqa: PLC0415
    from django.core.management import call_command  # noqa: PLC0415

    call_command("migrate", run_syncdb=True, verbosity=0)
    group = Group.objects.create(name="staff")
    for index in range(5):
        User.objects.create_user(f"user{index}", password="x").groups.add(group)  # noqa: S106 (test users)


def timed(run: Callable[[], object], repeat: int, warmup: int) -> list[float]:
    for _ in range(warmup):
        run()
    samples = []
    for _ in range(repeat):
        started = time.perf_counter_ns()
        run()
        samples.append((time.perf_counter_ns() - started) / 1000)
    return samples


def overhead(repeat: int) -> dict[str, dict[str, float]]:
    from asgiref.sync import async_to_sync  # noqa: PLC0415
    from django.test import AsyncClient, Client  # noqa: PLC0415

    client, async_client = Client(), AsyncClient()
    results = {}
    for name, path in VIEWS.items():
        samples = timed(lambda path=path: client.get(path), repeat, warmup=100)
        results[name] = {"median": statistics.median(samples), "p95": statistics.quantiles(samples, n=20)[18]}
    samples = timed(lambda: async_to_sync(async_client.get)(ASYNC_VIEW), repeat, warmup=100)
    results["async ORM N+1"] = {
        "median": statistics.median(samples),
        "p95": statistics.quantiles(samples, n=20)[18],
    }
    return results


def internal_state() -> dict[str, int]:
    """Sizes of tracepanel's process-wide containers: each must stay bounded however long the process runs."""
    from django.conf import settings  # noqa: PLC0415

    if "tracepanel.django" not in settings.INSTALLED_APPS:
        return {}
    from tracepanel.core import errors  # noqa: PLC0415
    from tracepanel.django import signals  # noqa: PLC0415
    from tracepanel.django import tasks as django_tasks  # noqa: PLC0415
    from tracepanel.integrations import celery  # noqa: PLC0415
    from tracepanel.storage import transport  # noqa: PLC0415

    writer = transport.get_writer()
    return {
        "logged exceptions": len(errors._logged),  # noqa: SLF001
        "celery runs": len(celery._runs),  # noqa: SLF001
        "celery published": len(celery._published),  # noqa: SLF001
        "django task runs": len(django_tasks._runs),  # noqa: SLF001
        "signal names": len(signals._names),  # noqa: SLF001
        "writer queue": writer._queue.qsize(),  # type: ignore[attr-defined]  # noqa: SLF001
        "writer dropped": writer.dropped,  # type: ignore[attr-defined]
    }


def flush() -> None:
    from django.conf import settings  # noqa: PLC0415

    if "tracepanel.django" in settings.INSTALLED_APPS:
        from tracepanel.storage import transport  # noqa: PLC0415

        transport.get_writer().flush()


def handlers() -> tuple[Callable[[str], object], Callable[[str], object]]:
    """GET through Django's own WSGI and ASGI handlers.

    The test Client connects signal receivers on every request, which would show up as growth that isn't tracepanel's.
    """
    from asgiref.sync import async_to_sync  # noqa: PLC0415
    from django.core.handlers.asgi import ASGIHandler  # noqa: PLC0415
    from django.core.handlers.wsgi import WSGIHandler  # noqa: PLC0415
    from django.test.client import RequestFactory  # noqa: PLC0415

    wsgi, asgi, factory = WSGIHandler(), ASGIHandler(), RequestFactory()

    def start_response(status: str, headers: object, exc_info: object = None) -> Callable[[bytes], object]:
        del status, headers, exc_info
        return lambda data: data

    def get(path: str) -> object:
        response = wsgi(factory.get(path).environ, start_response)  # type: ignore[bad-argument-type]  # a stub
        response.close()
        return response

    async def aget(path: str) -> None:
        headers: list[tuple[bytes, bytes]] = []
        scope = {"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": headers}
        body: list[Mapping[str, Any]] = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive() -> Mapping[str, Any]:
            if body:
                return body.pop()
            # The client stays connected: Django cancels this wait once the response is sent.
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: Mapping[str, Any]) -> None:
            del message

        await asgi(scope, receive, send)

    return get, async_to_sync(aget)


def memory(total: int, step: int) -> dict[str, object]:
    get, aget = handlers()
    paths = [*VIEWS.values()]
    tracemalloc.start()
    checkpoints = []
    baseline = None
    for done in range(0, total + 1, step):
        if done:
            for index in range(step):
                if index % 10 == 0:
                    aget(ASYNC_VIEW)
                else:
                    get(paths[index % len(paths)])
        flush()
        gc.collect()
        current, _ = tracemalloc.get_traced_memory()
        snapshot = tracemalloc.take_snapshot()
        if baseline is None and done >= step:
            baseline = snapshot
        checkpoints.append({"requests": done, "traced_kib": round(current / 1024), **internal_state()})
    growth: list[str] = []
    if baseline is not None:
        final = tracemalloc.take_snapshot()
        growth = [
            f"{stat.size_diff / 1024:+.0f} KiB {stat.count_diff:+d} blocks {stat.traceback[0]}"
            for stat in final.compare_to(baseline, "lineno")[:8]
        ]
    return {"checkpoints": checkpoints, "top growth after the first step": growth}


def store_timings(repeat: int) -> dict[str, object]:
    from django.test import Client  # noqa: PLC0415

    from tracepanel.storage import transport  # noqa: PLC0415
    from tracepanel.storage.store import get_store  # noqa: PLC0415

    client = Client()
    paths = [*VIEWS.values()]
    # Fill past the limit (2000): the store rotates while the timings run on a full table.
    for index in range(3000):
        client.get(paths[index % len(paths)])
    transport.get_writer().flush()
    store = get_store()
    calls: dict[str, Callable[[], object]] = {
        "counts (menu, every refresh)": store.counts,
        "traces overview": lambda: store.traces_overview(""),
        "search traces": lambda: store.search_traces("kind:http"),
        "endpoints": lambda: store.list_endpoints(""),
        "queries": lambda: store.list_queries(""),
        "issues": lambda: store.list_issues(""),
        "logs": lambda: store.search_logs(""),
        "services": lambda: store.list_services(""),
        "group summary": lambda: store.summary("route:/n-plus-one/"),
        "known values": store.known_values,
    }
    timings = {name: round(statistics.median(timed(call, repeat, warmup=2)) / 1000, 1) for name, call in calls.items()}
    with store._connect() as db:  # noqa: SLF001
        tables = ("traces", "logs", "sql", "issue_traces", "peers")
        rows = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}  # noqa: S608
    size = sum(path.stat().st_size for path in store.path.parent.glob(f"{store.path.name}*"))
    return {"ms (median)": timings, "rows": rows, "store size MiB (with WAL)": round(size / 2**20, 1)}


def child(mode: str, tracepanel: bool) -> None:  # noqa: FBT001
    setup(tracepanel=tracepanel)
    if mode == "overhead":
        result: object = overhead(repeat=int(os.environ.get("BENCH_REPEAT", "1000")))
    elif mode == "memory":
        result = memory(total=int(os.environ.get("BENCH_TOTAL", "12000")), step=3000)
    else:
        result = store_timings(repeat=20)
    print(json.dumps(result))


def run_child(mode: str, *, tracepanel: bool) -> object:
    with tempfile.TemporaryDirectory() as store_dir:
        env = {**os.environ, "TRACEPANEL_DIR": store_dir, "BENCH_CHILD": f"{mode}:{int(tracepanel)}"}
        output = subprocess.run(  # noqa: S603 (this interpreter, this script)
            [sys.executable, __file__], env=env, capture_output=True, text=True, check=True
        ).stdout
    return json.loads(output.strip().splitlines()[-1])


def main() -> None:
    if child_spec := os.environ.get("BENCH_CHILD"):
        mode, tracepanel = child_spec.split(":")
        child(mode, tracepanel=tracepanel == "1")
        return
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("modes", nargs="*", default=["overhead", "memory", "store"])
    args = parser.parse_args()
    if "overhead" in args.modes:
        off = run_child("overhead", tracepanel=False)
        on = run_child("overhead", tracepanel=True)
        if not (isinstance(off, dict) and isinstance(on, dict)):
            raise TypeError(off, on)
        print("\nOverhead per request, µs (median / p95)")
        print(f"{'view':22} {'without':>17} {'with tracepanel':>17} {'added':>10}")
        for name in on:
            a, b = off[name], on[name]
            print(
                f"{name:22} {a['median']:8.0f} / {a['p95']:6.0f} {b['median']:8.0f} / {b['p95']:6.0f}"
                f" {b['median'] - a['median']:+9.0f}"
            )
    if "memory" in args.modes:
        for tracepanel in (False, True):
            result = run_child("memory", tracepanel=tracepanel)
            print(f"\nMemory over many requests (tracepanel {'on' if tracepanel else 'off'})")
            print(json.dumps(result, indent=2))
    if "store" in args.modes:
        print("\nStore: console API on a full store")
        print(json.dumps(run_child("store", tracepanel=True), indent=2))


if __name__ == "__main__":
    main()
