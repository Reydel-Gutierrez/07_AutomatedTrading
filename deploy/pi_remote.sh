#!/usr/bin/env bash
# Pi side of scripts/deploy_pi.py. Runs as the SSH deploy user (not root, not
# the service user). Uses only these privileges:
#   sudo systemctl start|stop|restart|status agentic-portfolio.service
#   sudo -u agentic /opt/agentic-portfolio/.venv/bin/{python,pip} ...
# It never reads /etc/agentic-portfolio/env and never touches state/, logs/,
# reports/ or the venv contents beyond `pip install` of declared dependencies.
#
# Usage: pi_remote.sh <command> [args]
#   preflight                     JSON: deployed commit, service, market, in-flight orders
#   stage <sha> <tar-name>        unpack $DEPLOY_HOME/<tar-name> into a release dir
#   test <sha>                    run pytest on a scratch copy of the release (never in APP)
#   smoke <name>                  run scripts/pi_production_smoke.py as the service user; log to $DEPLOY_HOME/<name>
#   activate <sha> <meta-json>    back up APP code, stop, sync release, deps, start
#   verify <sha>                  service up on a fresh process, health ONLINE, files match release
#   rollback [backup-dir]         restore the newest (or given) backup, restart
#   cleanup <sha>                 drop the release dir, keep the newest backups
set -euo pipefail

APP=/opt/agentic-portfolio
VENV_PY="$APP/.venv/bin/python"
VENV_PIP="$APP/.venv/bin/pip"
SERVICE=agentic-portfolio.service
SERVICE_USER=agentic
DEPLOY_HOME="${AGENTIC_DEPLOY_HOME:-$HOME/agentic-deploys}"
RELEASES="$DEPLOY_HOME/releases"
BACKUPS="$DEPLOY_HOME/backups"
HISTORY="$DEPLOY_HOME/history.jsonl"
KEEP_BACKUPS=5

# Paths in APP that a code sync must never create, overwrite, or delete.
RSYNC_EXCLUDES=(
  --exclude=/.venv/ --exclude=/state/ --exclude=/logs/ --exclude=/reports/ --exclude=/var/
  --exclude=/src/*.egg-info/ --exclude=/DEPLOYED_COMMIT
  # Perishable: bytecode caches are never shipped or synced, but they do not keep
  # a deleted module's directory alive.
  "--filter=-p __pycache__/"
)

mkdir -p "$RELEASES" "$BACKUPS"

die() { echo "ERROR: $*" >&2; exit 1; }

svc() { sudo -n /usr/bin/systemctl "$1" "$SERVICE"; }

main_pid() { systemctl show -p MainPID --value "$SERVICE"; }

deployed_sha() {
  if [[ -f "$APP/DEPLOYED_COMMIT" ]]; then
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("sha") or "unknown")' "$APP/DEPLOYED_COMMIT"
  else
    echo unknown
  fi
}

cmd_preflight() {
  python3 - "$APP" "$SERVICE" "$(deployed_sha)" <<'PY'
import json, subprocess, sys, urllib.request
from pathlib import Path

app, service, sha = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
out = {"deployed_sha": sha}
out["service_active"] = subprocess.run(["systemctl", "is-active", service], capture_output=True, text=True).stdout.strip()
try:
    with urllib.request.urlopen("http://127.0.0.1:3100/healthz", timeout=5) as res:
        health = json.load(res)
    market = health.get("market") or {}
    out["health"] = {
        "agent": health.get("agent"),
        "phase": market.get("phase"),
        "regular_hours_open": bool(market.get("regular_hours_open")),
        "execution_mode": health.get("execution_mode"),
        "live_order_placement": health.get("LIVE_ORDER_PLACEMENT"),
    }
except Exception as exc:  # health is advisory here; the caller decides
    out["health"] = {"error": str(exc)}
# Any execution intent that is not terminal may be mid-submission. Restarting
# under it is exactly the ambiguity the executor's kill switch exists for.
terminal = {"FILLED", "REJECTED", "CANCELED", "BLOCKED_DISABLED", "BLOCKED", "REVALIDATION_REQUIRED"}
inflight = []
for path in sorted((app / "state" / "live_execution" / "intents").glob("*.json")):
    try:
        intent = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        inflight.append({"file": path.name, "status": "UNREADABLE"})
        continue
    if str(intent.get("status")) not in terminal:
        inflight.append({"file": path.name, "status": intent.get("status"), "symbol": intent.get("symbol")})
out["inflight_intents"] = inflight
ks = app / "state" / "runtime" / "kill_switch.json"
out["kill_switch_file"] = json.loads(ks.read_text(encoding="utf-8")) if ks.is_file() else None
print(json.dumps(out))
PY
}

cmd_stage() {
  local sha=$1 tar="$DEPLOY_HOME/$(basename "$2")" dir="$RELEASES/$1"
  rm -rf "$dir" && mkdir -p "$dir/code" "$dir/test"
  tar -xf "$tar" -C "$dir/code"
  tar -xf "$tar" -C "$dir/test"
  rm -f "$tar"
  echo "staged $sha"
}

cmd_test() {
  local dir="$RELEASES/$1/test"
  [[ -d $dir ]] || die "release $1 is not staged"
  # Declared runtime + dev dependencies of the release. Additive only: the running
  # service keeps working, and activation never has to wait on a download.
  mapfile -t deps < <(python3 -c 'import sys,tomllib; p=tomllib.load(open(sys.argv[1],"rb"))["project"]; print("\n".join(p.get("dependencies",[])+p.get("optional-dependencies",{}).get("dev",[])))' "$dir/pyproject.toml")
  if [[ -n ${deps[0]:-} ]]; then
    sudo -n -u "$SERVICE_USER" "$VENV_PIP" install --quiet "${deps[@]}"
  fi
  cd "$dir"
  # The venv's editable install points at APP/src; the release must shadow it.
  local loaded pkg
  for pkg in src/*/__init__.py; do
    pkg=$(basename "$(dirname "$pkg")")
    loaded=$(PYTHONPATH=src "$VENV_PY" -c "import $pkg, os; print(os.path.realpath($pkg.__file__))")
    [[ $loaded == "$(realpath "$dir")"/* ]] || die "tests would import $loaded instead of the release"
  done
  env -u AGENTIC_RUNTIME_MODE -u DASHBOARD_ENVIRONMENT -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
    "$VENV_PY" -m pytest -q -p no:cacheprovider
}

cmd_smoke() {
  local outfile="$DEPLOY_HOME/$(basename "$1")"
  cd "$APP"
  set +e
  sudo -n -u "$SERVICE_USER" "$VENV_PY" scripts/pi_production_smoke.py >"$outfile" 2>&1
  local rc=$?
  set -e
  cat "$outfile"
  return $rc
}

cmd_activate() {
  local sha=$1 meta=$2 code="$RELEASES/$1/code"
  [[ -d $code ]] || die "release $sha is not staged"
  local prev stamp backup
  prev=$(deployed_sha)
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  backup="$BACKUPS/${stamp}_${prev:0:12}"
  mkdir -p "$backup"
  rsync -a "${RSYNC_EXCLUDES[@]}" "$APP/" "$backup/"
  [[ -f "$APP/DEPLOYED_COMMIT" ]] && cp "$APP/DEPLOYED_COMMIT" "$backup/DEPLOYED_COMMIT"
  echo "backup: $backup"

  local deps_changed=0
  cmp -s "$code/pyproject.toml" "$APP/pyproject.toml" || deps_changed=1

  svc stop
  rsync -a --delete --checksum "${RSYNC_EXCLUDES[@]}" "$code/" "$APP/"
  if (( deps_changed )); then
    # Install declared runtime dependencies only. The editable .pth already
    # points at APP/src, so the package itself never needs reinstalling.
    mapfile -t deps < <(python3 -c 'import sys,tomllib; print("\n".join(tomllib.load(open(sys.argv[1],"rb"))["project"].get("dependencies",[])))' "$APP/pyproject.toml")
    if [[ -n ${deps[0]:-} ]]; then
      echo "pyproject.toml changed; installing: ${deps[*]}"
      sudo -n -u "$SERVICE_USER" "$VENV_PIP" install --quiet "${deps[@]}"
    fi
  fi
  python3 - "$APP/DEPLOYED_COMMIT" "$meta" "$prev" "$backup" <<'PY'
import json, sys
path, meta, prev, backup = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3], sys.argv[4]
meta.update(previous_sha=prev, backup=backup)
open(path, "w").write(json.dumps(meta, indent=2) + "\n")
PY
  date +%s >"$RELEASES/$sha/activated_at"
  svc start
  python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); m["event"]="deploy"; print(json.dumps(m))' "$APP/DEPLOYED_COMMIT" >>"$HISTORY"
  echo "activated $sha (previous $prev)"
}

cmd_verify() {
  local sha=$1 code="$RELEASES/$1/code" since
  since=$(cat "$RELEASES/$sha/activated_at") || die "release $sha was not activated"
  local pid="" ok=0
  for _ in $(seq 1 30); do
    sleep 2
    pid=$(main_pid)
    [[ $(systemctl is-active "$SERVICE") == active && $pid != 0 ]] || continue
    if python3 - "$APP/state/runtime/health.json" "$since" "$pid" <<'PY'
import json, sys
from datetime import datetime
health = json.load(open(sys.argv[1]))
since, pid = float(sys.argv[2]), int(sys.argv[3])
started = datetime.fromisoformat(str(health.get("started_at"))).timestamp()
sys.exit(0 if health.get("agent") == "ONLINE" and int(health.get("pid") or 0) == pid and started >= since else 1)
PY
    then ok=1; break; fi
  done
  (( ok )) || die "service did not report ONLINE on a fresh process within 60s (pid=$pid)"
  echo "service ONLINE pid=$pid"
  # Every file in the release must be byte-identical in APP.
  local mismatched
  mismatched=$(cd "$code" && find . -type f \
      -not -path './state/*' -not -path './logs/*' -not -path './reports/*' -print0 \
    | xargs -0 sha1sum | (cd "$APP" && sha1sum --quiet -c - 2>/dev/null) | wc -l) || true
  [[ $mismatched == 0 ]] || die "$mismatched file(s) in $APP differ from release $sha"
  [[ $(deployed_sha) == "$sha" ]] || die "DEPLOYED_COMMIT does not name $sha"
  echo "files match release $sha"
  curl -fsS -m 5 http://127.0.0.1:3100/healthz >/dev/null || die "/healthz not answering"
  echo "dashboard /healthz OK"
}

cmd_rollback() {
  local backup=${1:-}
  [[ -n $backup ]] || backup=$(ls -1d "$BACKUPS"/*/ 2>/dev/null | sort | tail -1)
  backup=${backup%/}
  [[ -n $backup && -d $backup ]] || die "no backup to roll back to"
  svc stop
  rsync -a --delete --checksum "${RSYNC_EXCLUDES[@]}" "$backup/" "$APP/"
  if [[ -f "$backup/DEPLOYED_COMMIT" ]]; then cp "$backup/DEPLOYED_COMMIT" "$APP/DEPLOYED_COMMIT"; else rm -f "$APP/DEPLOYED_COMMIT"; fi
  svc start
  python3 -c 'import json,sys,datetime; print(json.dumps({"event":"rollback","backup":sys.argv[1],"at":datetime.datetime.now(datetime.timezone.utc).isoformat()}))' "$backup" >>"$HISTORY"
  echo "rolled back to $backup (deployed_sha=$(deployed_sha))"
}

cmd_cleanup() {
  rm -rf "${RELEASES:?}/$1"
  ls -1d "$BACKUPS"/*/ 2>/dev/null | sort | head -n -"$KEEP_BACKUPS" | xargs -r rm -rf
  echo "cleaned"
}

cmd=${1:-}; shift || true
case $cmd in
  preflight) cmd_preflight ;;
  stage) cmd_stage "$@" ;;
  test) cmd_test "$@" ;;
  smoke) cmd_smoke "$@" ;;
  activate) cmd_activate "$@" ;;
  verify) cmd_verify "$@" ;;
  rollback) cmd_rollback "$@" ;;
  cleanup) cmd_cleanup "$@" ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
