#!/bin/sh
# Athena container entrypoint.
#
# One container runs three things:
#   1. this script -> uvicorn (public, 0.0.0.0:${PORT})   [PID 1 after exec]
#   2. a supervised crew_server (loopback 127.0.0.1:9001) [background]
#   3. `python -m athena.run` per research run            [spawned by the API]
#
# The crew server is supervised here rather than by the platform: it is a child
# of this container, so a crash would otherwise silently turn every run into a
# replay. The supervisor restarts it with a short delay (backing off to a minute
# on a crash loop), and stops restarting once the stop file exists (the API's
# shutdown hook creates it).
#
# Then `exec uvicorn`, on purpose: uvicorn becomes PID 1, so the platform's
# SIGTERM reaches the web server directly and its lifespan shutdown (active run
# subprocess + crew server) actually runs. Nothing here writes a secret.
set -eu

APP_DIR="${ATHENA_APP_DIR:-/app}"
DATA_DIR="${ATHENA_DATA_DIR:-$APP_DIR/data}"
PORT="${PORT:-8080}"
CARD_URL="${ATHENA_CREW_CARD_URL:-http://127.0.0.1:9001/.well-known/agent-card.json}"
CREW_READY_TIMEOUT="${ATHENA_CREW_READY_TIMEOUT_S:-20}"
CREW_RESTART_DELAY="${ATHENA_CREW_RESTART_DELAY_S:-3}"
CREW_MAX_BACKOFF="${ATHENA_CREW_MAX_BACKOFF_S:-60}"
CREW_HEALTHY_RUN_S="${ATHENA_CREW_HEALTHY_RUN_S:-60}"
START_CREW="${ATHENA_START_CREW:-1}"
PID_FILE="${ATHENA_CREW_PID_FILE:-$APP_DIR/.run/crew.pid}"
STOP_FILE="${ATHENA_CREW_STOP_FILE:-$APP_DIR/.run/crew.stop}"

cd "$APP_DIR"

# The volume must exist and be writable before anything else starts: the SQLite
# databases, the encrypted settings vault (data/settings/master.key) and the
# corpus all live there. Ownership is fixed at image build; a fresh volume is
# created root-owned by the platform, so make it ours when we can.
mkdir -p "$DATA_DIR" "$DATA_DIR/settings" "$DATA_DIR/tmp" \
         "$APP_DIR/.run" "$APP_DIR/.run/runs" "$APP_DIR/.run/api" "$APP_DIR/.run/mpl"
if [ ! -w "$DATA_DIR" ]; then
  echo "[entrypoint] FATAL: $DATA_DIR is not writable by uid $(id -u);" \
       "the volume must be mounted and owned by the app user" >&2
  exit 1
fi

wait_for_card() {
  python - "$CARD_URL" "$CREW_READY_TIMEOUT" <<'PY'
import sys
import time
import urllib.request

url, timeout = sys.argv[1], float(sys.argv[2])
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:
            if response.status == 200:
                print("[entrypoint] crew server ready", flush=True)
                raise SystemExit(0)
    except SystemExit:
        raise
    except Exception:
        time.sleep(0.5)
print("[entrypoint] crew server did not answer within %ss; serving anyway" % timeout,
      file=sys.stderr, flush=True)
raise SystemExit(1)
PY
}

supervise_crew() {
  rm -f "$STOP_FILE"
  (
    delay="$CREW_RESTART_DELAY"
    while [ ! -f "$STOP_FILE" ]; do
      started=$(date +%s)
      # stdout/stderr are this container's, so the platform's log collector
      # keeps the crew server's lines (JSON, like the API's).
      python -m athena.agents.crew_server &
      child=$!
      printf '%s\n' "$child" > "$PID_FILE"
      rc=0
      wait "$child" || rc=$?
      rm -f "$PID_FILE"
      if [ -f "$STOP_FILE" ]; then
        echo "[entrypoint] crew server stopped on request (rc=$rc)" >&2
        break
      fi
      # A server that stayed up for a while gets the short delay again. A crash
      # loop (for example no LLM_API_KEY yet) backs off, so a misconfigured
      # container does not restart the crew server every three seconds for hours.
      ran=$(( $(date +%s) - started ))
      if [ "$ran" -ge "$CREW_HEALTHY_RUN_S" ]; then
        delay="$CREW_RESTART_DELAY"
      fi
      echo "[entrypoint] crew server exited (rc=$rc, ran ${ran}s); restarting in ${delay}s" >&2
      sleep "$delay"
      delay=$(( delay * 2 ))
      if [ "$delay" -gt "$CREW_MAX_BACKOFF" ]; then
        delay="$CREW_MAX_BACKOFF"
      fi
    done
  ) &
  echo "[entrypoint] supervising crew server (pid $!)" >&2
}

if [ "$START_CREW" = "0" ]; then
  echo "[entrypoint] ATHENA_START_CREW=0: no crew server (replay-only deployment)" >&2
else
  supervise_crew
  wait_for_card || true
fi

echo "[entrypoint] starting uvicorn on 0.0.0.0:${PORT}" >&2
exec uvicorn athena.api.app:create_app --factory \
  --host 0.0.0.0 --port "${PORT}" \
  --proxy-headers --forwarded-allow-ips='*' \
  --log-level "${ATHENA_LOG_LEVEL:-info}" \
  --timeout-graceful-shutdown "${ATHENA_SHUTDOWN_TIMEOUT_S:-10}"
