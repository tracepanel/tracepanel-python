# Event schema

The source of truth for every part of tracepanel. Types for Python (SDK), Rust (agent) and TypeScript (UI) are generated from it.

The schema is defined in [JSON Schema](https://json-schema.org/).

## Files

| File | Contents |
| --- | --- |
| `trace.schema.json` | A trace with its spans, logs, errors, links and detected issues |
| `envelope.schema.json` | A batch of traces from one process, with project, environment, release and service |

Times are Unix epoch microseconds (UTC), durations are microseconds, ids are ULIDs.

## Entities

- **Trace** — an HTTP request, a background task or a management command (`kind`). A task is its own trace with a `link` to the span that enqueued it.
- **Span** — a unit of work inside a trace. `kind` is a closed set (`http`, `sql`, `cache`, `http.client`, `template`, `task.enqueue`, `task.run`, `command`, `function`); `attrs` are open and namespaced (`http.route`, `db.fingerprint`, `cache.hit`, `code.origin`).
- **Log** — a log record bound to a span.
- **Error** — an exception with frames, bound to a span.
- **Issue** — a detected problem (`n+1`, `duplicates`, `slow-sql`, `error`) with a stable `fingerprint`, shared by local mode, the agent and the cloud.
- **Envelope** — a batch with schema version, SDK, project, environment, release, service, host and process.
