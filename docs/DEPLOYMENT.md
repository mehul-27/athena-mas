# Deployment

> **Status: not deployed.** Everything in this repository is *deployment-ready
> configuration*. No cloud resource has been created, no billing account has been
> touched, no image has been pushed and no CLI has logged in anywhere. Deployment
> happens only when the user explicitly authorises it (see
> [Remaining user actions](#remaining-user-actions)).

## What runs in the container

One image, one machine, one volume. Three processes:

| Process | Address | Started by | Job |
| --- | --- | --- | --- |
| `uvicorn athena.api.app:create_app --factory` | `0.0.0.0:${PORT}` (8080) | `docker/entrypoint.sh` (`exec`, so it is PID 1) | the demo API and the static page |
| `python -m athena.agents.crew_server` | `127.0.0.1:9001` (loopback) | the same entrypoint, supervised | the A2A analyst/writer/critic |
| `python -m athena.run` | child of the API | `athena/api/runs.py` | one research run at a time |

The crew server is supervised rather than platform-managed: if it dies, every
live run would silently degrade. The entrypoint's supervisor restarts it with a
delay that backs off from 3s to 60s on a crash loop, and stops restarting once
`ATHENA_CREW_STOP_FILE` exists (the API's shutdown hook writes it). Its
stdout/stderr are the container's, so the platform's log collector keeps them.

**Why one machine.** The active run lives in an in-memory `RunRegistry`, the run
subprocess is a child of the API process, `POST /api/runs` accepts one run at a
time, and SQLite plus the encrypted vault live on a single volume. A second
instance would serve a different universe and could not see the other's runs.
`fly.toml` therefore pins `min_machines_running = 1` with autostop/autostart off,
and a persistent volume can only be attached to one machine anyway.

## What was blocking a public deployment, and what fixed it

| Blocker | Fix |
| --- | --- |
| `athena/api/guards.py` hardcoded `ALLOWED_HOSTS = {localhost, ...}`, so every write returned `400` behind a public host | Host allowlist comes from `ATHENA_ALLOWED_HOSTS` (comma list, `*.fly.dev` and `.example.com` wildcards). Default unchanged. Origin check uses the same list; a same-origin write is always allowed |
| `/api/health` was the only probe: always `200` and it did a blocking 1s HTTP call | `GET /healthz` (liveness, no I/O) and `GET /readyz` (volume writable, DB openable, crew card with a 5s cache). Both are exempt from the token gate. `/api/health` is untouched for the UI |
| No token: every endpoint stored provider keys, spawned processes and spent tokens | `athena/api/auth.py`: optional shared-token middleware, enabled only when `ATHENA_ACCESS_TOKEN` is set. Bearer, Basic, `X-Athena-Token` or the cookie from `/login?token=`. Constant-time compare |
| No lifespan hooks: `SIGTERM` killed the container mid-run | `create_app` now has a lifespan: startup creates and verifies the writable directories; shutdown stops the run subprocess (`RunRegistry.terminate_all`) and the child crew server, each with a timeout |
| `POST /api/runs` had no `guard()` | it calls `guard(request)` before starting anything |
| `cryptography` and `httpx` were imported but unpinned | pinned additively in `requirements.txt` (`cryptography==50.0.2`, `httpx==0.28.1`, the versions installed in `.venv`) |
| No structured logging | `configure_logging()` installs a JSON formatter via `logging.basicConfig` (non-forcing) and the 500 handler logs with `log.exception` |
| The vault key (`data/settings/master.key`) must survive a restart | it lives under `ATHENA_DATA_DIR`, which is the mounted volume; `.dockerignore` keeps it out of the image |

## Persistence map

Everything the app writes is under the volume mounted at `/app/data`:

| Path | Contents | Survives restart |
| --- | --- | --- |
| `/app/data/events.db` | the event log (`EVENTS_DB`) | yes - one row per event, append-only |
| `/app/data/blackboard.db` | task/plan/evidence/draft/critique/report versions | yes |
| `/app/data/chat.db` | chat conversations and messages | yes |
| `/app/data/settings/master.key` | vault key (Fernet) | yes - **lose it, lose every stored provider key** |
| `/app/data/settings/secrets.enc.json` | encrypted provider keys | yes (needs the key above) |
| `/app/data/settings/profiles.json`, `active_profile.json` | provider profiles, active profile | yes |
| `/app/data/chroma/`, `/app/data/corpus/` | vector index and source documents | yes |
| `/app/data/fastembed_cache/` | embedding model cache | yes (avoids a re-download) |
| `/app/data/ratelimit.db`, `ratelimit_out.db` | cross-process token limiters | yes (safe to lose) |
| `/app/data/tmp/` | temp files (`TMPDIR`, `TEMP`, `TMP`) | no, scratch |
| `/app/.run/runs/*.log`, `*.summary.json` | per-run subprocess logs | **no** - container filesystem |
| `/app/.run/api/*.png` | generated heatmaps | no, regenerated per request |

After a restart, run history (`GET /api/runs`) still comes from `events.db` and
`blackboard.db`; only the *raw subprocess log tail* of an old run is gone, and
`GET /api/runs/{id}/log` falls back to an empty list for it.

## Environment variables (names only - values are never in this repo)

Deployment-critical:

| Name | Purpose |
| --- | --- |
| `ATHENA_ACCESS_TOKEN` | enables the token gate. **Must be set on a public host** |
| `ATHENA_ALLOWED_HOSTS` | extra Host/Origin values, e.g. the public hostname |
| `PORT` | port uvicorn binds (Fly: 8080; Render injects its own) |
| `ATHENA_DATA_DIR` | volume root (`/app/data`) |
| `ATHENA_START_CREW` | `0` disables the crew server (replay-only deployment) |
| `ATHENA_LOG_FORMAT` | `json` (default) or `text` |
| `ATHENA_LOG_LEVEL` | root log level, and uvicorn's |
| `ATHENA_VERSION`, `ATHENA_GIT_SHA` | reported by `/readyz` and the startup line |
| `ATHENA_SHUTDOWN_TIMEOUT_S` | grace period for the run subprocess and the crew server |
| `ATHENA_CREW_CARD_URL`, `ATHENA_CREW_READY_TIMEOUT_S`, `ATHENA_CREW_RESTART_DELAY_S`, `ATHENA_CREW_MAX_BACKOFF_S`, `ATHENA_CREW_HEALTHY_RUN_S`, `ATHENA_CREW_PID_FILE`, `ATHENA_CREW_STOP_FILE` | entrypoint/supervisor wiring (card URL, pre-uvicorn wait, restart delay, backoff cap, "ran long enough" threshold, pid and stop files) |
| `ATHENA_CARD_TIMEOUT_S`, `ATHENA_CARD_CACHE_S` | `/readyz` card probe timeout and cache TTL |
| `LLM_MODEL`, `LLM_API_BASE`, `LLM_API_KEY` | provider (a key can also live in the encrypted vault) |
| `SEARCH_PROVIDER`, `ATHENA_SEARXNG_URL` | web search |
| `EVENTS_DB`, `BLACKBOARD_DB`, `CHAT_DB`, `RATELIMIT_DB`, `RATELIMIT_OUT_DB`, `CHROMA_PATH`, `CORPUS_DIR`, `FASTEMBED_CACHE_PATH`, `MPLCONFIGDIR`, `TMPDIR`/`TMP`/`TEMP` | every writable path, pointed at the volume by the image |

The run-limit and per-role token caps (`ATHENA_MAX_LLM_CALLS`,
`LLM_ITPM_LIMIT`, ...) are documented in `.env.example`; the image does not
override them.

## Secrets handling

* Nothing secret is in the image: `.dockerignore` excludes `.env*`,
  `data/settings/**`, `**/master.key`, `**/secrets.enc.json`, `*.db`, `.venv/`
  and `.git/`, and the Dockerfile copies only `athena/`, `scripts/`,
  `data/demo/`, `data/eval/` and the entrypoint. No `COPY . .`, no `ARG` secret.
* Provider keys have two supported homes: the platform's secret store
  (`fly secrets set LLM_API_KEY=...`) or the encrypted vault
  (`data/settings/master.key` + `secrets.enc.json` on the volume, written by the
  settings UI). The vault is the one the UI manages; a key set through the UI is
  never returned by any endpoint.
* `ATHENA_ACCESS_TOKEN` belongs in platform secrets. Treat it as the only lock
  on an API that can spend money and rewrite local state; rotate it with
  `fly secrets set ATHENA_ACCESS_TOKEN=<new>` and sign in again.
* Logs are JSON and redacted by the application's existing `redact()`; no
  endpoint echoes a key, and the auth layer never logs a candidate token.

## Deploying to Fly.io (the recommended target)

Non-mutating checks only, from this workstation: `fly`/`flyctl` are **not
installed**, so nothing was deployed, validated or logged in. `docker` 29.6.2 is
installed and was used for the local build/smoke test described below.

The commands the user runs (in order, all of them authorized-only):

```sh
# 1. one-time, authenticated as the user
fly auth login
fly apps create athena-mas                       # or: fly launch --no-deploy --copy-config
fly volumes create athena_data --size 10 --region iad

# 2. secrets (never on the command line in a shared shell history)
fly secrets set ATHENA_ACCESS_TOKEN=... ATHENA_ALLOWED_HOSTS=athena-mas.fly.dev
fly secrets set LLM_MODEL=... LLM_API_BASE=... LLM_API_KEY=...   # optional here; the UI vault also works

# 3. the volume is created root-owned; give it to the app user (uid 10001)
fly ssh console --user root -C "chown -R 10001:10001 /app/data"

# 4. deploy a tagged image, so rollback is a tag, not a rebuild
fly deploy --build-arg ATHENA_VERSION=0.1.0 \
           --build-arg ATHENA_GIT_SHA=$(git rev-parse HEAD) \
           --image-label "athena-$(git rev-parse --short HEAD)"
```

`fly.toml` already pins: `internal_port = 8080`, `force_https = true`,
`min_machines_running = 1`, `auto_stop_machines = false`,
`auto_start_machines = false`, HTTP checks on `/healthz` every 15s (60s grace),
`[[vm]] shared-cpu-2x / 2gb`, `kill_timeout = 30s` and the volume
`athena_data -> /app/data`.

## Render (documented alternative, not chosen)

`render.yaml` describes the same shape: a Docker web service, `plan: starter`,
`numInstances: 1`, `healthCheckPath: /healthz`, one 10GB disk at `/app/data`,
and `sync: false` for the two per-deployment secrets. Differences that matter:
Render injects `PORT` itself (the image reads it), the free tier cannot attach a
disk, and there is no `fly ssh` equivalent for the one-time
`chown` of the volume - use a `preDeployCommand` or a one-off shell with root.
Nothing in the repo connects a Blueprint to the repository; that is a user
action in the Render dashboard.

## Smoke test checklist (run after *any* deploy, before announcing it)

1. `curl -fsS https://<host>/healthz` returns `{"status":"ok"}` in well under a
   second.
2. `curl -sS https://<host>/readyz` shows `status: ready`, `checks.volume.ok`,
   `checks.database.ok`, `checks.crew_server.ok` and the expected `version` /
   `commit`.
3. `curl -s -o /dev/null -w '%{http_code}' https://<host>/api/questions`
   is `401` (token gate on) - and `200` with `-H "Authorization: Bearer $TOKEN"`.
4. A browser hitting `/` lands on `/login`; entering the token lands on the app
   and the page still works (no remote assets, no CORS errors).
5. One live run end to end: history row appears, the event log streams, the
   heatmap renders, the final report and critic verdict are shown.
6. A run with the crew server killed mid-flight (`fly ssh console` ->
   `pkill -f crew_server`) fails visibly rather than hanging, and `/readyz`
   flips to `degraded: true` within ~5s.
7. Restart the machine (`fly apps restart athena-mas`): `/api/runs` still lists
   the previous runs, and a provider key stored through the UI is still there
   (that proves the volume holds `events.db` and `data/settings/master.key`).
8. `fly logs` shows JSON lines with `event=startup` and `event=shutdown`, and no
   key or token anywhere.
9. During a stop, the shutdown log shows the run subprocess and crew server
   stopped *before* the process exits.

## Rollback

1. **Image**: every deploy can be tagged (`--image-label`). Roll back with
   `fly releases` to pick the previous version, then
   `fly deploy --image <registry>/athena-mas:<previous-tag>` (or
   `fly releases rollback`). Rollback is safe for the databases because this
   change is additive (no schema migration).
2. **Volume**: take a snapshot before a deploy that could touch data
   (`fly volumes snapshots create athena_data`). Restore by creating a new
   volume from the snapshot (`fly volumes create athena_data --snapshot-id <id>`
   into a new name, then re-point the mount) - never restore over the live one.
3. **Config regression** (e.g. a bad `ATHENA_ALLOWED_HOSTS`): `fly secrets unset`
   / `fly secrets set` and restart; no image change needed.
4. **Worst case**: `fly apps destroy athena-mas` (destroys the volume too - the
   snapshot from step 2 is the backup).

## Cost

| Item | Estimate |
| --- | --- |
| Fly `shared-cpu-2x`, 2GB, always on, one machine | ~$10-14/mo (usage-based; ~$0.000018/s) |
| Fly volume, 10GB | ~$1.50/mo, plus ~$0.02/GB of snapshot |
| Egress | usually a few cents for a demo |
| **Total platform** | **~$9-15/mo**, matching the brief |
| Render alternative (starter + disk) | ~$7-10/mo, similar shape |
| **LLM spend** | **separate and not included.** Per run: 12 model calls at the configured budgets (`ATHENA_MAX_LLM_CALLS`), dominated by the researcher/writer/critic. On a small hosted tier that is single-digit cents per run; a reasoning model is several times more. Rate limits (`LLM_ITPM_LIMIT`, `LLM_OTPM_LIMIT`) keep a runaway run bounded |
| Web search | DuckDuckGo is keyless; SearXNG self-hosted is free but needs its own host |

## Remaining user actions

Deployment is **not** done, and this repository deliberately stops before any
action that would create or bill a resource. To go live, the user must:

1. **Authorise the deployment** (this document is a proposal, not a report of a
   completed deploy).
2. **Choose the provider** - Fly.io is recommended and configured; Render is the
   documented alternative. Fly CLI is not installed on this workstation.
3. **Approve billing** for the chosen account (a paid instance is required; free
   tiers cannot attach a persistent volume).
4. **Authenticate** the CLI (`fly auth login`) - nothing was logged in.
5. **Create the app and the volume** (`fly apps create`, `fly volumes create`).
6. **Set the secrets** with `fly secrets set`: `ATHENA_ACCESS_TOKEN`,
   `ATHENA_ALLOWED_HOSTS`, and the provider variables (`LLM_MODEL`,
   `LLM_API_BASE`, `LLM_API_KEY`) or store the key in the UI vault instead.
7. **Chown the volume** to uid 10001 once, from a root console.
8. **Run the deploy command** from the previous section, then the smoke test
   checklist.
9. Decide whether to rotate the access token after the demo, and who else gets
   it.

## What was validated here, and what was not

* `pytest tests/test_deploy_*.py` - host allowlist, token gate (allow/deny/
  exempt/login cookie), `/healthz` + `/readyz` behaviour and caching, the
  lifespan hooks, `terminate_all`, the JSON formatter, and static checks of the
  Dockerfile, `.dockerignore`, entrypoint, `fly.toml`, `render.yaml`, CI.
* The full existing suite (UI, chat, settings, guards) still passes unchanged.
* `docker build` (Docker 29.6.2, `python:3.13-slim`) **succeeded**: the image is
  1.92GB and installs cleanly from `requirements.txt` with no compiler (only
  `libgomp1` from apt).
* Container smoke test, run locally and then removed:
  * `/healthz` -> `{"status":"ok"}`; `/readyz` -> `ready` with
    `checks.volume.ok`, `checks.database.ok` under `/app/data`.
  * with `LLM_API_KEY` set: the supervisor logged `crew server ready`, the crew
    server bound `127.0.0.1:9001`, uvicorn bound `0.0.0.0:8080`, and `/readyz`
    reported `degraded: false` with `crew_server.ok: true`.
  * the token gate: anonymous `GET /api/questions` -> `401`, `Bearer` -> `200`,
    wrong token -> `401`, `/login?token=...` -> the app shell; `HEALTHCHECK`
    reported `healthy` with `restarts=0`.
  * the container ran as uid `10001`; `/app/data` was owned by `athena`.
  * `docker stop` completed in 1.3s with
    `shutdown: crew server stopped` in the JSON log, and the supervisor printed
    `crew server stopped on request (rc=143)` - the child exited on SIGTERM
    instead of being killed.
* **Known behaviour, by design:** with no `LLM_API_KEY` the crew server cannot
  start (it refuses to build without a key). The supervisor then logs
  `crew server exited (rc=1); restarting in Ns` and backs off 3s -> 6s -> 12s
  -> 24s -> 48s -> 60s. `/readyz` stays `200` with `degraded: true` and
  `crew_server.ok: false`, so the page still loads, replay works, and the
  platform does not restart-loop the container. Set the key (secret or UI
  vault) before showing live runs.
* **Not** validated: a real Fly/Render deploy, TLS/proxy behaviour in front of
  the app, volume chown on a platform-created volume, and the first-run
  `fastembed` model download inside the container's network policy.
