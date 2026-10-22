# Changelog

## Unreleased

### Added

- Huey is traced, plain or through `huey.contrib.djhuey`, like Celery, RQ and
  Dramatiq (`task.system` is `huey`). Each task a consumer runs is a `task`
  trace, linked to the `task.enqueue` span of the code that enqueued it, with
  its arguments, result, retries, state, worker, queue wait, `eta`, priority
  and the `timeout` that stopped it. A pipeline's steps, a chord's callback
  and any task enqueued from a task's run record the run they follow
  (`task.parent_id`, `task.root_id`), and a group's or chord's tasks share a
  `task.group_id`. The Tasks page's Live lists them while they are queued,
  running or waiting to be retried, and lists each consumer. Immediate mode
  runs a task inside its caller: it gets a trace and no Live events. Huey's
  messages carry nothing extra, so the link to the enqueue waits in the
  instance's storage under `tracepanel:<task id>` until the task starts.
  `huey_enabled = false` turns it off.
- A query a template made as it rendered (a lazy QuerySet its `{% for %}`
  reads, a related object it shows) has the template's line
  (`code.template_line`), Django's templates and Jinja's alike, and
  `code.template` names the file that line is in: a page that extends
  another names the base template for a query outside its own blocks. The
  console marks these queries and, with an agent that knows the line, an
  N+1 a template made names the template and its line in its hint.
- Task workflows link up across Celery, RQ, Dramatiq and Django Tasks. A
  run records the task it follows (`task.parent_id`), the first task of its
  workflow (`task.root_id`) and its group (`task.group_id`): Celery's chain,
  group and chord ids, RQ's `depends_on` (every id in `task.depends_on`) and
  Groups, Dramatiq's pipelines and groups, and any task sent from inside
  another's run. A step sent from inside the run before it (a Celery chain or
  `link`, a chord's callback, a Dramatiq pipeline or callback, a job enqueued
  by a job) links to that run, so the console lists the steps as related.
- RQ's `on_success`, `on_failure` and `on_stopped` callbacks run as traces of
  their own, linked to their job's.
- django-q2: every task a cluster runs is a `task` trace (`task.system` is
  `django-q`), and `async_task` and `schedule` are `task.enqueue` spans the
  run links to, as for Celery and RQ. A run says its cluster, its worker
  process, how many runs of it came back before (`task.retries`), its group,
  and the limit when its `timeout` stopped it. A chain's next step and a task
  sent from inside a run follow that run (`task.parent_id`, `task.root_id`),
  a `hook` runs as a trace of its own linked to its task's, and what the
  scheduler enqueues is a scheduled run. `sync=True` runs inside the caller
  like an eager Celery task. Task events and worker heartbeats feed the Tasks
  page's Live. `django_q_enabled` turns it off.
- A run stopped by its time limit says the limit in `task.timeout` (seconds):
  Celery's soft limit (SoftTimeLimitExceeded), RQ's `job_timeout`
  (JobTimeoutException) and Dramatiq's TimeLimit (TimeLimitExceeded). Celery's
  hard limit kills the pool process before the run can say anything: the
  worker's main process now writes its trace, failed with TimeLimitExceeded,
  and tells the Tasks page's Live that it finished.
- RQ, Dramatiq and Django Tasks traces name their worker (`task.worker`), as
  Celery's do: RQ's worker name, Dramatiq's worker process (the name its task
  events give) and the Django Tasks backend's worker id.
- Every queue says when a delayed task was due (`task.eta`, ISO 8601) and its
  priority (`task.priority`), on the run's trace as well as on the enqueue:
  Celery's eta or countdown and message priority, RQ's `enqueue_at` and
  `enqueue_in`, Dramatiq's delay and actor priority, Django Tasks'
  `run_after` and priority.
- The app's settings and versions reach the console, for its App tab (needs
  an agent that serves `api/app`). The hello to the agent carries them, once
  per process: Django's UPPERCASE settings and Flask's `app.config`, each
  value as `repr()` shows it, cut long. A setting whose name holds a secret
  (`SECRET_KEY`, `*PASSWORD*`, `*TOKEN*`, `*KEY*`, `*SIGNATURE*`) is masked
  whole, and so is such a key inside one: the `PASSWORD` in `DATABASES`, a
  password in a cache's `OPTIONS`. The credentials in a URL are masked
  whatever its name: a Sentry DSN reads
  `https://********@o1.ingest.sentry.io/1`.
  `PASSWORD_HASHERS`, `AUTH_PASSWORD_VALIDATORS` and the like stay readable.
  The versions are Python's, the framework's, and those of the packages that
  matter that are installed: the database drivers, the ORM, the task queues,
  the servers, tracepanel itself, and for Django the packages its
  `INSTALLED_APPS` come from. FastAPI, Starlette and Falcon apps send the
  versions only. `capture_settings` turns the settings off.
- What Django finds wrong with the app reaches the console's App tab too
  (needs an agent that keeps an app's checks). Its system checks, as
  `manage.py check` reports them, the deployment ones too when DEBUG is off:
  each with its id, level, message, hint and object, the silenced ones and
  those that query the database left out. The migrations not applied to the
  default database yet, by app, as `runserver` warns of them. And the model
  changes with no migration, as `makemigrations --check` finds them. They
  are worked out once per server process, in a thread of their own, from the
  first connection to the agent on: the hello carries them once known, and
  the agent connected then is told in a frame. A database that doesn't
  answer leaves the migrations applied unknown, and an in-memory one is left
  alone. A management command other than a server doesn't run them.
- A template span keeps the context the template got, as Django's template
  debug panels show it: its names in order, each value as an error's local
  variables show theirs (`repr()`, cut, under a sensitive name masked, the
  credentials in a URL too), 40 names at most (`template.context.*`). Django
  templates have the context processors' entries (`csrf_token` masked), an
  included template its own context, and Django's own `block` and `forloop`
  are left out. Jinja templates have what `render()` got, in Flask,
  Starlette, FastAPI and Django's Jinja2 backend alike. Showing a value never
  runs a query: a QuerySet shows its model, and its row count only once it
  ran, a lazy object stays lazy, and a value whose `repr()` would query reads
  as not shown. A template included in a loop keeps the context of its first
  3 renders, a trace 100 in all. They stay on this machine.
  `capture_template_context` turns them off.
- Settings and a template's context show their strings in double quotes,
  `"django.db.backends.postgresql"`, as one writes them, still Python a
  reader can paste.
- FastStream: each message a subscriber handles is a task trace, named
  after the handler's function, on Kafka (aiokafka and Confluent), RabbitMQ,
  NATS and Redis. The trace says the broker, the topic, queue, subject or
  channel, the message's id, how many times it was delivered before when the
  broker says, and the outcome: an exception is an error with its locals, a
  nack the broker delivers again is a retry. SQL, HTTP calls, Redis commands
  and logs inside the handler land in its trace. A publish inside a trace is
  a `task.enqueue` span, and the message's headers carry the trace context
  (`traceparent`), so the handler's trace links to the trace that published
  it. A message a handler publishes follows its run, as a task sent from
  another's run does (`task.parent_id`, `task.root_id`), and a RabbitMQ
  message says its priority (`task.priority`). The Tasks page's Live lists
  the messages queued and handled, and the app as a worker (`service@host`).
  It works in a FastStream app, under FastStream's FastAPI routers and with
  `faststream-fastapi`.
- Django signals say more. A send's span is named by the signal: Django's
  own by their name (`post_save`), the project's and other libraries' by
  their dotted path (`shop.signals.order_placed`). It carries the sender
  (`shop.Order` for a model) and the line of the project that sent it
  (`code.origin`, as for SQL), or the library's line when no project code
  did. Each receiver's span has the receiver's own file and line, and the
  exception it raised: `send_robust()` swallows it, so the trace stays ok
  and the receiver's span shows the error. A send nobody receives makes no
  span: the trace's `signal.unreceived` counts it, once per signal and
  sender (`pre_save shop.Order: 3`). Django's lifecycle signals
  (`request_started`, `request_finished`, `setting_changed`,
  `connection_created`, `pre_migrate`, `post_migrate`) show only when a
  receiver of the project's listens, with Django's own beside it: sent to
  nobody or to Django's own receivers alone (`create_permissions` after
  every migrate), they make no span and are not counted. When a receiver of
  the project's listens to `request_started`, what it does opens the
  request's trace.
- `signals_enabled` turns the signal spans off, and `ignore_signals` leaves
  signals out by name or dotted path (glob patterns). By default it holds
  `pre_init` and `post_init`, which Django sends twice for every object a
  query loads, and `class_prepared`, sent for every model class built (each
  historical model a `migrate` renders): they keep Django's own send and
  cost nothing.

### Changed

- A signal's span is named `post_save`, not `signal post_save`, and a model
  sender reads `auth.Group`, not `django.contrib.auth.models.Group`.

### Fixed

- A query a Jinja template made, in a template file inside the project, has
  the line of project code that rendered it as its `code.origin`. It had the
  template's file with a line of Jinja's compiled code.
- RQ jobs scheduled with `enqueue_at` or `enqueue_in`, or enqueued in a batch
  with `enqueue_many` (and Groups), have their enqueue span, their link to it
  and their `enqueued` event. Only `enqueue` had them.
- A delayed Dramatiq message keeps how its send was triggered. Moved to its
  queue when due, it said `other`. Its `task.eta` is a time, like Celery's,
  not `+500 ms`.
- A Dramatiq message retried while its next attempt already ran in another
  worker thread no longer says `finished` for the attempt that failed. The
  Tasks page's Live dropped the running attempt.
- A Celery run killed by its hard time limit has one trace, the failed one.
  Its pool process, sent SIGTERM before it is killed, wrote a second trace
  that read ok and said `finished` for it, and the console could show that
  one. The pool process now writes nothing of a run it leaves unfinished: the
  worker's main process writes it. A run cut short by a cold shutdown or
  `revoke(terminate=True)` reads REVOKED with Terminated, one whose pool
  process died fails with WorkerLostError. They read ok, or had no trace.

## 0.4.1 (2026-10-21)

### Fixed

- A request's profile shows its sync view or endpoint under an ASGI server.
  Django runs a sync view through `sync_to_async`, and FastAPI and Starlette
  run a sync endpoint through `run_in_threadpool`, in a worker thread. The
  profile sampled the thread the request started in, the event loop's, which
  waited in its selector meanwhile: the flame graph showed
  `KqueueSelector.select` (or `EpollSelector.select`) and none of the view.
  The profile now follows the work into the worker thread, and leaves out the
  loop's thread while it waits. Async views and WSGI apps were not affected.
- A Celery task's arguments read the same while queued and while running in
  the Tasks page's Live, and in its trace. The enqueued event showed the
  arguments the caller passed (`('a',)`), the started event and the trace
  what the worker decoded from the message (`['a']`). All three now show
  Celery's own `argsrepr` and `kwargsrepr`, which the message carries, so an
  app that hides a secret there hides it in Tracepanel too. Django Tasks'
  traces showed their arguments as the backend stored them (`['a']`), its
  events as a tuple: the trace now shows the tuple too. RQ and Dramatiq
  already matched.
- A process forked while the writer was sending to the agent no longer hangs
  its traces and task events. A worker that forks (RQ's work horse for each
  job, Celery's prefork pool, gunicorn with `--preload`) copies only the
  thread that forks: a lock the parent's writer thread held at that moment
  (sending a batch or a heartbeat, starting the agent) stayed held in the
  child, so the child's writer waited forever and its jobs never reached the
  console. Every lock of Tracepanel's is free again in a forked child. The
  child already opened its own connection to the agent, with its own hello.
  It now also closes its copy of the parent's.
- A task event goes to the agent at once, not after the writer's 50 ms wait
  for more traces. A job shorter than that sent `started` and `finished`
  together, and the Tasks page's Live never showed it running.

## 0.4.0 (2026-10-21)

Needs `tracepanel-agent` 0.4.0 (installed with it): an app says what it offers
the console in its hello, and the agent answers `api/capabilities`.

### Added

- Celery tasks in flight reach the console before they end: the integration
  sends small task events to the agent (enqueued with the trace that sent
  it, started, retried with the error, finished), and every worker process
  a heartbeat with the tasks it runs. The Tasks page lists the tasks queued,
  running and waiting to be retried, and the workers. The hello says
  `live_tasks`. Eager tasks send none, and `capture_task_args` and
  `ignore_tasks` apply to the events too.
- RQ, Dramatiq and Django Tasks send the same task events, so the Tasks
  page's Live lists their jobs, messages and tasks in flight and their
  workers too. A worker says it is there as it starts (RQ, Dramatiq) or with
  its first task (Django Tasks). Dramatiq's retries are told before the next
  attempt can start. Jobs of an RQ queue with `is_async=False` and tasks of
  Django's ImmediateBackend and DummyBackend send none.
- Falcon's ASGI app (`falcon.asgi.App`) is traced as its WSGI app is:
  `tracepanel run uvicorn app:app`, or `tracepanel.falcon.install(app)` in
  code. A request is named after its route's URI template, its responder
  runs in a `view` span, and an error Falcon turns into a 500 is recorded.
  Pages get the panel, and the console is served under `/__tracepanel__/`.
- Falcon: a request whose responder runs a Strawberry operation is named
  after it (`GraphQL query PostList`), as in Django, Flask and FastAPI.
- A streamed HTML page gets the panel too: Django's `StreamingHttpResponse`
  (sync or async), Starlette's and FastAPI's `StreamingResponse`, a
  generator in Flask or any WSGI app. The script goes in right after the
  `<head>` (or `<body>`) opens, as the pieces pass, and nothing else is
  held: only a tag cut between two pieces waits for its end. A streamed
  response also gets the badge's headers, with the time so far. The panel
  shows the full time once the trace is stored. A compressed page, or one
  that declares its size, is left as it is.
- Flask, FastAPI and Starlette record the session and the signed-in user,
  as Django does. Flask's session is recorded as the view left it, and so is
  the user Flask-Login loaded. Starlette's are the ones its
  `SessionMiddleware` and `AuthenticationMiddleware` put on the request.
  Secrets in the session stay masked, Flask-Login's session hash among them.
- A query no project code made (Django's admin, a paginator, a template tag)
  records the library code that asked for it as `code.caller`: the first
  frame past the ORM and the driver. The panel shows it where it showed
  nothing.
- A page's navigation answered with a redirect (a form's POST, then 302) is
  named in a short-lived `tracepanel_redirect` cookie, so the panel on the page
  it leads to lists it first.
- A request records what it carried, as django-debug-toolbar's Request and
  Headers panels show it: its headers and the response's, its query, its
  cookies, the body as far as the app read it (a form's files by name and
  size), and the session the view used. Values under a secret's name
  (`password`, `token`, `sessionid`, `Authorization`, `Set-Cookie`…) are
  masked, and none of it leaves the machine. Tracepanel never reads a body
  the app didn't. `capture_request = false` (or
  `TRACEPANEL_CAPTURE_REQUEST=0`) turns it off. The console's
  `api/capabilities` says whether it is on.
- A request's profile: where its time went in code, sampled while it runs,
  for the console's flame graph. A request asks for it with an
  `X-Tracepanel-Profile: 1` header, or the panel's
  `tracepanel_profile=1` cookie, set while its Profile toggle is on.
  `profile = true` profiles every trace, `profile_enabled = false` none, and
  `profile_interval_ms` sets how often it samples (every millisecond by
  default). The console's
  capabilities say whether profiling is on.

### Changed

- What the app offers the console is said once, in the hello to the agent:
  its service, what its traces hold (`profile`, `request`) and what its
  console answers (`explain`, Django's `users`). The console's
  `api/capabilities` comes from the agent, which offers EXPLAIN and switching
  users only through the app's console. The app's console no longer answers it
  itself, and connects to the agent first, so the agent has heard the hello.

### Fixed

- A log record's level is always one of the five every SDK shares (debug,
  info, warning, error, critical), taken from its logging number. A library's
  own level (loguru's TRACE or SUCCESS, a custom NOTICE) went out under its
  own name, which the schema and the console's level filter don't know.
- An ASGI server may pass the request's headers, client and server as any
  iterable, not only lists. Tracepanel read them more than once, so the
  second read found nothing. It now reads them once. The headers it adds to
  a response have lowercase names, as the ASGI spec asks.
- The first trace after the agent restarted was lost (the panel said
  "Trace not found"): the old connection still took the write on this side.
  Before each batch, the writer checks whether the agent closed the
  connection, and connects again at once if it did.
- A profiled request could come back with no profile. The sampler thread,
  idle long enough to leave, left as a new trace started, and nothing
  sampled it. The decision to leave is now taken under the lock that
  starting a trace takes.
- While traces are profiled, the GIL changes hands as often as the sampler
  wakes (every millisecond). Before, a request busy in Python code let the
  sampler run only every 5 to 30 ms. The interpreter's own interval comes
  back after the last profiled trace.

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
