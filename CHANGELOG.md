# Changelog

## 0.5.0 (2026-10-26)

Needs `tracepanel-agent` 0.5.0 (installed with it).

### Added

- A pytest plugin turns what the agent finds into test failures.
  `pytest --tracepanel`, or `tracepanel = true` in the pytest settings, runs
  each test as a `test` trace, and an issue of a kind in `tracepanel_fail_on`
  (`n+1` and `duplicates` by default) fails the test with its title, query,
  call site and a link to the trace. A request made through Django's,
  Starlette's, FastAPI's, httpx's or Flask's test client is a trace of its
  own, `called_by` the test's. `@pytest.mark.tracepanel` takes
  `max_queries`, `fail_on` and `allow`, the `tracepanel_trace` fixture has
  `.queries` and `.issues`, and `tracepanel.max_queries(n)` caps a block. It
  works with pytest-xdist, pytest-django and async tests. The plugin starts
  the project's agent when none runs. Without one it warns once and checks
  query counts only.
- LLM calls made with the official `openai` and `anthropic` SDKs are spans,
  sync and async, streamed or not: OpenAI's chat completions, responses and
  embeddings, and Anthropic's messages (`messages.stream()` too). A span is
  named `chat gpt-5` or `chat claude-sonnet-5-5` and holds OpenTelemetry's
  GenAI attributes: the provider, the operation, the model asked for and the
  one that answered, the request's settings, the finish reasons and the
  tokens used, cached and reasoning ones included. The tools the model
  called are named and counted. A stream's span lasts until it is read,
  closed or dropped, with its time to first chunk. The HTTP calls of the
  call, one per attempt, are inside it. A rate limit or an overloaded API is
  an error with its exception and status. The messages are kept only with
  `capture_llm_content = true` (off by default), masked and cut at
  `max_body_kb`. `openai_enabled = false` and `anthropic_enabled = false`
  turn them off. Anthropic's own OpenTelemetry spans are left out while
  tracepanel records its calls. The spans are of their own kind, `llm`. The
  agent estimates their cost from list prices, and the `llm_prices` setting
  names a JSON file of prices that override or add models, which the agent
  tracepanel starts reads (`--llm-prices`).
- AWS calls are spans of their own, through botocore's clients (boto3,
  aiobotocore): `S3 PutObject avatars/1.png`, `DynamoDB Query users`, with
  the service, operation, region, request id, botocore's retries and the
  error code of a failed call (`NoSuchKey`), as OpenTelemetry names them.
  S3 calls are `storage` spans (`storage.system` s3, the bucket and key as
  the path), DynamoDB's are `dynamodb` commands with their request's shape
  as the statement and the values apart, SQS's SendMessage is an `sqs`
  message, SNS's Publish a `messaging` one, and any other service an `rpc`
  call. The N+1 and duplicate checks apply to them. The HTTP requests of a
  call are part of its span, not calls of their own. A call made for a
  Django storage (django-storages' S3Storage), a cache or a task queue is a
  span of its own kind inside that call, which the agent counts as the
  layer's, so one loop is counted once. The requests upload_file() and
  download_file() send from s3transfer's threads are in the trace.
  `boto_enabled = false` turns it off.
- Django's `migrate` shows what it did: each migration it applied or
  unapplied is a span, `migrate shop 0012_add_sku` (`unapply` going back),
  with whether it ran in a transaction and whether it was faked. Each of its
  operations is a span inside, `AddField Product.sku`,
  `RunPython backfill_sku`, holding the SQL the schema editor ran for it or
  the queries its code made, and the rows its INSERT, UPDATE and DELETE
  statements changed. The trace has the plan and how many migrations were
  applied and unapplied. An operation that locks or rewrites a table on the
  database it runs on says why in `migration.risk`: on PostgreSQL a column
  type change that isn't binary coercible, a column made NOT NULL and an
  AddIndex without CONCURRENTLY (or a RemoveIndex unapplied), on MySQL a
  column type change, and anywhere a RunPython or RunSQL without its
  reverse. `sqlmigrate` runs nothing and gets no spans.
  `migrations_enabled = false` turns it off. The spans are of their own
  kinds, `migration` and `migration.operation`.
- Taskiq is traced like Celery, Dramatiq and Huey (`task.system` is
  `taskiq`), through a middleware tracepanel puts first on every broker. Each
  `.kiq()` in a trace is a `task.enqueue` span, and each task a worker runs is
  a `task` trace linked to it, with its arguments, result, retries, state,
  worker, queue wait, the `eta` its `delay` label gives, its `priority` label
  and the `timeout` label that stopped it. A retry middleware's retry is
  `RETRY`, a task kicked from a task's run follows it (`task.parent_id`,
  `task.root_id`) and the scheduler's tasks run with the trigger `schedule`.
  The Tasks page's Live lists them while they are queued, running or waiting
  to be retried, and lists each `taskiq worker` process. A profiled request's
  tasks are profiled too, an async one down to what it awaits. The
  InMemoryBroker runs a task in its caller's process: it gets a trace and no
  Live events. `taskiq_enabled = false` turns it off.
- Django's file storages are traced: each call a storage makes to its
  backend (save, open, delete, exists, listdir, size, the modified, accessed
  and created times) is a span, `storage save avatars/1.png`, with the
  storage's alias in STORAGES, its class and system (`filesystem`, `s3`,
  `gcs`, `azure`…), the path and the bytes written or read when the file
  knows them. Every Storage class is covered, Django's, django-storages' and
  the project's own, and a call made inside another (save asking exists for
  a free name) is part of it. An S3 request's HTTP call lands inside its
  span. url() is a span where it does more than join strings, on S3 where it
  signs the URL. `collectstatic` shows where its time went: a span for each
  phase (clear, copy with the finders' listing, post-process), the 10
  slowest files of each with their storage calls, and how many files were
  copied, linked, unmodified and post-processed. `storage_enabled = false`
  turns it off. The calls are `storage` spans, collectstatic's phases and
  files `function` spans.
- A GraphQL request records its server library, `graphql.library`
  (`strawberry` or `graphene`), on the trace and the operation's span. The
  agent names that library's DataLoader in an N+1's hint.
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
- SQL Server: statements run through pyodbc and pymssql are traced like the
  other drivers' (`db.system` "mssql"), with N+1 and duplicate detection on
  `?`, `%s` and `%(name)s` placeholders. Under Django (mssql-django) and
  SQLAlchemy they are recorded once, by the ORM, and Django's atomic blocks
  are transactions. EXPLAIN shows SQL Server's estimated plan (SHOWPLAN_ALL),
  a full scan of a table marked, for the drivers, Django and SQLAlchemy.
- Channels' background work is a task, `task.system` `channels`, on the
  Tasks page and in its Live. A task is a message sent to a worker channel
  (a channel name without `!`, the kind `runworker` listens on): the send is
  a `task.enqueue` span, the worker's handling of it a `task` trace named
  after the consumer's handler, with its module (`ThumbnailConsumer`'s
  `thumbnails_generate` for type `thumbnails.generate`), and linked to the
  send. The sender says `enqueued`, the worker `started` and `finished`, and a
  `runworker` process sends heartbeats from its start, whatever the channel
  layer. WebSocket and HTTP consumers serve requests, and group sends reach
  them: neither is a task.
- A response's body is recorded with its request's, as Django Silk shows
  it: JSON and forms with their secrets masked as a request's are, and text,
  in `http.response.body` with its size in bytes. Only a response sent whole
  is read, never a stream (Django's StreamingHttpResponse, a streamed ASGI or
  WSGI response). A page, its scripts and stylesheets, binary content and
  compressed bodies are recorded by their size only. Both bodies are kept up
  to `max_body_kb` (64 KiB, was 16 KiB for a request's), marked as cut past
  it, and `capture_body = false` turns them off.
- A payment card's number and security code are masked wherever secrets
  are: names like `card_number`, `cardNumber`, `card[number]`, `card_no`,
  `pan`, `cvc`, `cvv` and `cvn` in bodies, form fields, headers, settings, a
  template's context and an error's local variables. In a body, a setting or
  a template's context, `number` under a card (`card`, `creditCard`,
  `payment_card`) is masked too, and so is a `number` (or `num`) beside a
  card's other fields (`cvc`, `cvv`, `exp_month`, `exp_year`, `expiry`)
  whatever its object is called, as in Stripe's
  `{"source": {"number": ...}}`. A `number`, `num` or `account` holding a
  card's digits (13 to 19, passing the Luhn check) is masked wherever it is.
- A query a template made as it rendered (a lazy QuerySet its `{% for %}`
  reads, a related object it shows) has the template's line
  (`code.template_line`), Django's templates and Jinja's alike, and
  `code.template` names the file that line is in: a page that extends
  another names the base template for a query outside its own blocks. The
  console marks these queries, and an N+1 a template made names the
  template and its line in its hint.
- Graphene: a request to graphene-django's GraphQLView is traced as its
  GraphQL operation, as Strawberry's are. The trace is named after the
  operation (`GraphQL query PostList`) and keeps its type, name, document and
  error count. Its span holds `parse` and `validate`, and each of the
  project's resolvers is a span of its own. Exceptions raised in resolvers are
  the trace's errors. A batch view runs one operation span per entry. It needs
  no GRAPHENE `MIDDLEWARE` entry, and `graphene_enabled = false` turns it off.
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
  sent from inside a run follow that run (`task.parent_id`, `task.root_id`).
  The group id django-q gives a chain's steps is not recorded: a chain is not
  a group. A `hook` runs as a trace of its own linked to its task's, and what
  the scheduler enqueues is a scheduled run. `sync=True` runs inside the
  caller like an eager Celery task. Task events and worker heartbeats feed the
  Tasks page's Live. `django_q_enabled` turns it off.
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
- The app's settings and versions reach the console, for its App tab. The
  hello to the agent carries them, once per process: Django's UPPERCASE
  settings and Flask's `app.config`, each value as `repr()` shows it, cut
  long. A setting whose name holds a secret
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
- What Django finds wrong with the app reaches the console's App tab too.
  Its system checks, as
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
- The server's access log leaves out the requests under `prefix`: the panel's
  polling no longer floods the terminal, and the app's own requests stay.
  This covers uvicorn, gunicorn, hypercorn, granian, daphne, and the Django
  and Flask development servers. `quiet_access_log = false` keeps them.
- A profile can count memory, with Python's tracemalloc, while a profiled
  trace runs: its `peak` above the level at the start, its `net` at the end,
  and the 50 `sites` that held the most, as the profile's frames down to the
  project's line, with the library's line it called. The sites are the
  picture at the peak when the sampler took one near it (`sites_at`), else at
  the end. `shared` says another trace ran in the process meanwhile. Ask for
  it with an `X-Tracepanel-Profile: memory` header, a
  `tracepanel_profile=memory` cookie, or `profile_memory = true` for every
  profiled trace. "1" profiles time alone, as before. tracemalloc slows the
  whole process down while a trace is counted, a short request several times
  over. It starts with the first trace that asks for memory and stops once
  none has for a while. An app's own tracemalloc is left as it is. A task
  started by a request that asked for memory carries `memory` instead of `1`,
  and the hello offers `profile_memory` next to `profile`, so the panel can
  ask for it.
- A profile's `gc` has the garbage collections during the trace, their pause
  and when each began. Traces that aren't profiled pay nothing.

### Changed

- A signal's span is named `post_save`, not `signal post_save`, and a model
  sender reads `auth.Group`, not `django.contrib.auth.models.Group`.
- The Redis commands channels-redis sends for itself are no longer `redis`
  spans: the sorted sets of a send or group send, their expiry and capacity
  checks, the publishes of the pub/sub layer and a receive's blocking pop.
  They are the channel layer's transport, recorded as its `channel.send` or
  `task.enqueue` span. A WebSocket event's trace had them as `redis` spans
  that made up most of its time, and a `redis` tag. Redis commands the app
  sends itself are recorded as before.

### Fixed

- A profiled trace always has its profile. One that ended before the
  profiler's first sample (a task of a few milliseconds, or one whose thread
  the profiler couldn't get to under load) had none, as if it hadn't been
  profiled. Its profile now says 0 samples, with its memory when that was
  asked for.
- The configuration a framework's install() sets is the one in use. A
  heartbeat or the writer that read the settings in its own thread meanwhile
  could put the configuration from before back, and leave the app's requests
  untraced.
- `tracepanel run` with an interpreter that has no tracepanel installed (a
  pyenv or system Python) only says it does not trace the process. It also
  printed an `Error in sitecustomize` about `importlib.machinery`, which the
  bootstrap used without importing it. The project's own sitecustomize still
  runs there.
- `tracepanel run` traces the database drivers of a FastAPI, Flask or Falcon
  app created before anything imported sqlite3. Creating the app printed
  `could not patch sqlite3` and left every driver untraced, sqlite3,
  psycopg and the rest.
- FastAPI: a Django `atomic()` block in a sync dependency with `yield` ends
  where Django commits it, with its outcome and duration. FastAPI enters the
  block in one worker thread's copy of the request's context and leaves it in
  another's. The block's span was kept in the first, so it never ended. The
  open blocks are now kept on the connection, as Django keeps them.
- The profiler no longer keeps the frames it samples alive until the next
  garbage collection. A request's locals, its rows, outlived it until then.
- A task worker that stops leaves the Tasks page's Now at once instead of
  staying there as an idle worker. It says so in a `worker_stopped` event
  as it stops cleanly: a Celery worker at `worker_shutdown`, an RQ worker as
  it unregisters, a Dramatiq worker process after its shutdown, a Huey
  process worker as it shuts down and thread workers as the consumer exits
  (a SIGTERM'd `run_huey`), django-q2's cluster for a worker process it
  replaced (recycled, stopped by its timeout, dead) and for each one as it
  stops, Django Tasks' `db_worker` as it ends, and a Channels `runworker` at
  Ctrl+C. A Celery worker's main process beats from its start, so an idle
  worker stays listed, and `db_worker` says it is there as it starts.
- A Django Task run on the immediate backend inside a request or command
  records its spans under its own root. They had the request's enqueue span
  as their parent, a span of another trace. A run's trace ends inside the
  `task_finished` signal's span, and that span's end no longer makes the
  run's root current again once the run is over. Any trace that begins or
  ends inside a span of another trace leaves the right span current.
- A SQL Server statement on `[dbo].[orders]` is named `SELECT dbo.orders`,
  as the agent names one it gets over OpenTelemetry. It was `SELECT dbo`.
  Every quoted name keeps its schema (`"SHOP"."ORDERS"` is `SHOP.ORDERS`), and
  IN lists of `@P1`, `@name`, `:1`, `:name`, `?1` and `$1` placeholders
  collapse like `%s`, `%(name)s` and `?`. EXPLAIN knows those placeholders and
  reads none inside a `[name]`.
- An app's processes that start the agent at the same moment, as django-q2's
  cluster does when it forks its workers, start one. Two could run on the
  store, and the processes on the first sent their traces and task events
  where the console didn't look. They now start it in turn, and one whose
  agent finds the store served sends to the agent that serves it.
- A GraphQL document that doesn't parse is named `GraphQL syntax error`,
  with no operation type or name, for Strawberry and Graphene. It was
  `GraphQL query anonymous`, so the GraphQL page grouped it with the
  anonymous queries. A document that fails validation keeps its operation's
  name, as before.
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
- A Celery run whose soft time limit passes while tracepanel starts its trace
  has its trace, and the next run in that worker process is not recorded
  into it. Celery logs the exception and runs the task. The run had no
  trace, or one that stayed current for the runs after it. Its trace now
  says the limit (`task.timeout`) and keeps SoftTimeLimitExceeded as a
  handled error. A run whose task_prerun never got to tracepanel is recorded
  from its end. So is a Dramatiq message whose time limit lands in
  tracepanel's own hooks.
- A Django project without `django.contrib.auth` or `contenttypes` in
  INSTALLED_APPS no longer fails at startup. tracepanel imported auth's
  models for user switching, and Django refused to load them. Without auth,
  user switching is left out of the console and logouts are not recorded.
  Requests are traced as usual.
- A Channels Redis layer configured with `("host", port)` pairs names its
  server on its spans. They said `localhost:6379`, whatever the host.
- A Celery run that ends without a state from Celery is FAILURE, with the
  error that ended it. That is a run cut short outside the task's body, say
  by a soft time limit hitting Celery as it imports its result backend, or
  an eager task whose error reaches its caller. It was stored with no state
  and status ok.
- Secrets are masked by the words of a name, not the letters in it. An `author`,
  `authors`, `author_id`, `authority`, `is_authenticated`, `tokenizer`, a
  model's `max_tokens` or a `passenger` is shown in bodies and an error's
  locals, where it read `********`. Passwords, tokens, keys,
  `Authorization`, `x-auth`, `csrftoken`, `sessionid` and cookies are masked
  as before, `PHPSESSID` too. A name about a secret (`password_length`,
  `token_type`, `cookie_name`) or a yes or no (`is_private`, `has_password`)
  is shown. Plural credentials are masked (`refresh_tokens`, `api_keys`,
  `client_secrets`), and so is `tokens` alone unless it holds a model's counts
  (`{"tokens": 1024}`).
- A query made through SQLAlchemy's asyncio (an `AsyncSession` in a FastAPI
  endpoint, a FastStream handler or any coroutine) has the line of project
  code that awaited it as its `code.origin`, and so has its transaction. The
  console said no caller was recorded: SQLAlchemy runs the query in a
  greenlet of its own, whose stack holds none of the app's frames. The
  stack of the greenlet that awaits it is read too.
- EXPLAIN works for a query made through SQLAlchemy's asyncio, on aiosqlite,
  asyncpg, psycopg, aiomysql and asyncmy. It failed with "greenlet_spawn has
  not been called". The plan is read on a connection made for it, in an
  event loop of its own, with the engine's URL, connect arguments and
  connect events. The app's pool and event loop are left alone: asyncpg's
  connections belong to the loop they were made on. An in-memory SQLite
  database is explained on its one connection.
- A task that a profiled request starts is profiled too. The Profile toggle
  profiled the request but not the Celery, Dramatiq, RQ, Huey, django-q2,
  FastStream or Channels task it queued, often where its time goes. The
  message carries a `tracepanel_profile` flag next to its link to the
  enqueue (`tracepanel-profile` in FastStream's headers), and the worker
  profiles the run, and the tasks that run sends in turn. Only a trace that
  asked to be profiled hands it on: with `profile = true` every trace is
  profiled anyway. A task run inside its caller is in the caller's profile.
  Django Tasks carry nothing to the worker, so their runs aren't profiled,
  and calls to other apps over HTTP don't carry the flag.
- An async trace's profile shows what its code awaits, not the idle event
  loop. A FastStream task that awaited `asyncio.sleep` read as 99% in
  `selectors`, and traces sharing an event loop lost their samples to each
  other. A trace begun in an asyncio task is that task's: while the task
  waits, its sample is the task's coroutines down to what it awaits, the
  app's innermost coroutine with the line of its `await` (`at_line`). Frames
  that wait (a sleep, a socket read, a lock, select, `time.sleep` and other
  C calls) say `waiting`. Each stack says when in the trace it was seen, in
  10 ms buckets (`at`, `bucket_us`), and ticks the profiler can't tell
  between traces on one thread are counted (`dropped`).

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
