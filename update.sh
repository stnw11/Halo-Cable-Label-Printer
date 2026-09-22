#!/usr/bin/env bash
#
# Update the service on the Docker host. Run it from anywhere:
#
#     /opt/halo-cable-label-printer/update.sh
#
# It pulls, reports any new settings the update brought, waits for the queue
# to drain, rebuilds and restarts, then waits until the service is healthy.
#
# It never writes .env or config/*.yaml. Those are gitignored, so an update
# cannot overwrite them; this script only tells you when the templates have
# gained a setting your deployment does not have. Editing them stays a
# human's job -- a deployment should never end up running on a value nobody
# chose.
#
# Flags:
#   --no-pull   rebuild what is already on disk (offline or tarball installs)
#   --now       restart without waiting for in-flight jobs to finish
#
set -euo pipefail

cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

PULL=1
WAIT_FOR_QUEUE=1
for arg in "$@"; do
  case "$arg" in
    --no-pull) PULL=0 ;;
    --now)     WAIT_FOR_QUEUE=0 ;;
    -h|--help) awk 'NR>1{ if (!/^#/) exit; sub(/^# ?/, ""); print }' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

compose() { docker compose "$@"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if [[ ! -f .env ]]; then
  echo "No .env here. This is an update script, not an installer -- see DEPLOYMENT.md." >&2
  exit 1
fi

# --- 1. New code -------------------------------------------------------------
if [[ $PULL -eq 1 && -d .git ]]; then
  step "Fetching the update"
  # --ff-only so a deployment that someone has edited locally stops here and
  # says so, rather than opening a merge on a production host.
  if ! git pull --ff-only; then
    echo "git pull could not fast-forward. This checkout has local commits or" >&2
    echo "changes; sort those out first, or run with --no-pull to rebuild as-is." >&2
    exit 1
  fi
elif [[ $PULL -eq 1 ]]; then
  echo "Not a git checkout; rebuilding what is on disk (same as --no-pull)."
fi

# --- 2. What the update expects that you do not have -------------------------
step "Checking your config against the templates"
python3 tools/check_config.py || true    # advisory: every new setting has a default

# --- 3. Don't cut a job in half ----------------------------------------------
# A restart between reserving identifiers and writing the job would consume
# numbers that never reach a cable. The service is built to leave gaps rather
# than collisions, so this is survivable -- but waiting a few seconds for the
# queue to drain avoids it entirely.
if [[ $WAIT_FOR_QUEUE -eq 1 ]] && compose ps --status running --quiet 2>/dev/null | grep -q .; then
  step "Waiting for in-flight jobs to finish"
  QUEUE_ROOT="$(grep -E '^QUEUE_ROOT=' .env | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')"
  QUEUE_ROOT="${QUEUE_ROOT:-/mnt/cable-queue}"
  for _ in $(seq 1 120); do   # up to 10 minutes; a 250-cable batch is ~1 minute
    outstanding="$(compose exec -T halo-cable-label-printer sh -c \
      "ls -1 '$QUEUE_ROOT'/inbox '$QUEUE_ROOT'/processing 2>/dev/null | grep -c . || true" 2>/dev/null | tr -d '[:space:]')"
    [[ -z "${outstanding:-}" || "$outstanding" == "0" ]] && break
    printf '\r  %s job file(s) still in flight; waiting...' "$outstanding"
    sleep 5
  done
  echo -e "\r  queue is clear                              "
fi

# --- 4. Rebuild and restart --------------------------------------------------
step "Rebuilding"
compose build

# Starting is the step that can fail for a reason outside this host: the
# queue share is a CIFS volume, and Docker mounts it when the container
# starts. If the print host is off, the container cannot start at all --
# and because it never ran, `restart: unless-stopped` will not retry it.
# An update begun while the print host was asleep would otherwise leave the
# service stopped with a daemon error and no explanation.
step "Starting"
# mktemp, not a fixed /tmp path: this script runs as root on the Docker
# host, and a predictable name in a world-writable directory lets any local
# user pre-plant a symlink and have root truncate the file it points at.
START_ERR="$(mktemp "${TMPDIR:-/tmp}/cable-label-start.XXXXXX")"
trap 'rm -f "$START_ERR"' EXIT
SHARE_WAIT="${SHARE_WAIT_MINUTES:-2}"
deadline=$(( SECONDS + SHARE_WAIT * 60 ))
until compose up -d 2>"$START_ERR"; do
  if ! grep -qi 'mounting volume\|mount error\|failed to mount' "$START_ERR"; then
    cat "$START_ERR" >&2          # some other failure; don't loop on it
    exit 1
  fi
  host="$(grep -E '^SMB_HOST=' .env | tail -1 | cut -d= -f2- | tr -d '"'"'"' ')"
  if (( SECONDS >= deadline )); then
    echo >&2
    echo "Could not mount the queue share from ${host}, so the service cannot start." >&2
    echo "The print host is unreachable -- powered off, asleep, or its network" >&2
    echo "profile has reset to Public, which blocks SMB. Bring it back, then run:" >&2
    echo "    ./update.sh --no-pull" >&2
    echo "Nothing was lost: identifiers are only consumed once a job is written." >&2
    exit 1
  fi
  printf '\r  waiting for the queue share on %s (print host offline?)...' "$host"
  sleep 15
done

# --- 5. Prove it came back ---------------------------------------------------
step "Waiting for the service to report healthy"
for _ in $(seq 1 30); do
  status="$(compose ps --format '{{.Status}}' | head -1)"
  case "$status" in
    *healthy*)   echo "  $status"; break ;;
    *unhealthy*|*Exited*|*Restarting*) echo "  $status" >&2; compose logs --tail 30; exit 1 ;;
    *) printf '\r  %s' "$status"; sleep 5 ;;
  esac
done

step "Recent log"
compose logs --tail 8

cat <<'DONE'

Updated. Your .env and config/*.yaml were not touched.
If the check above listed new settings, add the ones you want by hand and
run this script again with --no-pull.
DONE
