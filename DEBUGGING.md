# Compute logging and API diagnostics

Set these in the compute backend's private `.env`:

```dotenv
LOG_LEVEL=info
DEBUG=false
```

| Level | What it records | When to use it |
|---|---|---|
| `debug` | Request-start traces plus request summaries, upload/pipeline diagnostics and detailed browser-console traces | Investigating a specific failure; enable temporarily |
| `info` | Startup, API completion summaries (method, route template, status, duration, request ID), job starts/results, and failures | Default for normal operation |
| `error` | HTTP 4xx/5xx summaries, unhandled exceptions by type, job preparation/process failures | When only failed operations are needed |

`DEBUG=true` is a legacy alias that overrides `LOG_LEVEL` and enables debug.
Use `DEBUG=false` when selecting `info` or `error`. Pipeline diagnostic helpers
inherit the same environment. Third-party pipeline output, Uvicorn/Nginx logs,
and the activity audit trail have their own logging behavior; this setting
controls the application logger, not every line produced by dependencies.

## Apply a change

Run from `cem-backend`:

```bash
docker compose up -d api frontend
docker compose logs -f api
```

Recreate containers after an environment change; `restart` alone does not apply
new environment. Refresh the compute page afterward. `LOG_LEVEL=debug` also
enables browser diagnostics through generated `/runtime-debug.js`; only a boolean
flag reaches the browser, not credentials. See the compute frontend DEBUGGING.md
for per-tab and localStorage overrides.

## Log locations

| Log | Host default | Purpose |
|---|---|---|
| `app.log` | `data/logs/cem-backend/app.log` | API and compute application events; rotates at 10 MiB with five backups |
| `activity.jsonl` | `data/logs/cem-backend/activity.jsonl` | Existing user-action audit trail; independent of `LOG_LEVEL` |
| `_run.log` | Within `data/projects/<project>/.../results/<step>/` | Subprocess output for a pipeline task |

Compose mounts the host log directory at `/logs`. `HOST_LOG_DIR` overrides the
host location; retain `LOG_DIR=/logs` inside the container. Existing installations
with an explicit `HOST_LOG_DIR` retain that path. New app logs survive container
recreation. If the directory is not writable, application logging continues on
stdout and reports `logging.file_unavailable`.

```bash
tail -f data/logs/cem-backend/app.log
docker compose exec -T api tail -f /logs/app.log
```

## Read an API request summary

```text
[INFO] [cem.compute] http.finish {"elapsed_ms": 125.4, "method": "POST", "request_id": "example-id", "route": "/api/v1/analyze", "status": 200}
```

A 4xx/5xx response uses `[ERROR]`. An exception also records `http.error` with
its type and the same request ID. The middleware observes status/timing without
consuming uploads or buffering audio. It uses route templates, so paths such as
`/jobs/{job_id}` do not expose the concrete ID in request summaries. Headers,
query strings, request/response bodies and exception messages are not recorded
by the request logger. A route not matched by FastAPI is labeled `<unmatched>`.

For sir's diagnosis: reproduce once, check the browser Network request's method
and status, then inspect the corresponding route/time in `app.log`. Use `debug`
for detailed browser and pipeline traces if the `info` summary is insufficient.

The activity trail includes user identity supplied by the request headers; it is
not evidence of validated Google SSO. Treat audit files and raw pipeline logs as
private operational data and review them before sharing.
