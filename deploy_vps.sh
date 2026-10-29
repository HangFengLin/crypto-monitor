#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-crypto-vps}"
REMOTE_DIR="${REMOTE_DIR:-/opt/crypto-project}"
SERVICE="${SERVICE:-crypto-project}"
BUILD_ONLY="${BUILD_ONLY:-}"
ENABLE_OKX_ORDER_MODE="${ENABLE_OKX_ORDER_MODE:-}"
COMPOSE_FILES="-f docker-compose.yml"

if [[ -n "${ENABLE_OKX_ORDER_MODE}" || " ${SERVICE} " == *" okx-strategy-bot "* ]]; then
  echo "OKX robot deployment is retired; deploy crypto-project for paper signal tracking." >&2
  exit 2
fi

# This path publishes an already-built, independently verified image only.
# It never synchronizes source or starts other Compose services.
if [[ -n "${VPS_OVERLAY_RELEASE:-}" ]]; then
  if [[ "${VPS_OVERLAY_RELEASE}" != "yes" || "${SERVICE}" != "crypto-project" || -n "${BUILD_ONLY}" ]]; then
    echo "Overlay requires VPS_OVERLAY_RELEASE=yes, SERVICE=crypto-project and no BUILD_ONLY." >&2
    exit 2
  fi
  candidate="${VPS_OVERLAY_CANDIDATE_ID:-}"
  accepted="${VPS_OVERLAY_ACCEPTED_IMAGE_ID:-}"
  expected="${VPS_OVERLAY_EXPECTED_IMAGE_ID:-}"
  site_tag="${VPS_OVERLAY_SITE_TAG:-}"
  recovery_tag="${VPS_OVERLAY_RECOVERY_TAG:-}"
  image_id_pattern='^sha256:[0-9a-f]{64}$'
  image_tag_pattern='^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*$'
  if [[ ! "$candidate" =~ $image_id_pattern || ! "$expected" =~ $image_id_pattern ||
        "$candidate" != "$accepted" || "$candidate" == "$expected" ||
        ! "$site_tag" =~ $image_tag_pattern || ! "$recovery_tag" =~ $image_tag_pattern ||
        "$site_tag" == "$recovery_tag" || "$REMOTE_DIR" != "/opt/crypto-project" ||
        "$REMOTE_HOST" != "crypto-vps" ]]; then
    echo "Invalid overlay image IDs, acceptance, tags or deployment directory." >&2
    exit 2
  fi
  ssh "$REMOTE_HOST" bash -s -- "$candidate" "$expected" "$site_tag" "$recovery_tag" <<'REMOTE_OVERLAY'
set -euo pipefail
candidate=$1
expected=$2
site_tag=$3
recovery_tag=$4
base=/opt/crypto-project/docker-compose.yml
override=/opt/crypto-project/deploy/jindan-v1/web-override.yml
fail() { echo "Overlay preflight failed: $1" >&2; exit 1; }
details=$(sudo -n docker inspect --format '{{.Image}}|{{.Config.Image}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{index .Config.Labels "com.docker.compose.project.config_files"}}' crypto-project) || fail 'running website is unavailable'
[[ "$details" == "$expected|$site_tag|crypto-project|crypto-project|/opt/crypto-project|$base,$override" ]] || fail 'running image or Compose identity changed'
[[ "$(sudo -n docker image inspect "$candidate" --format '{{.Id}}')" == "$candidate" ]] || fail 'candidate image missing'
[[ "$(sudo -n docker image inspect "$recovery_tag" --format '{{.Id}}')" == "$expected" ]] || fail 'recovery tag missing or mismatched'
[[ "$(sudo -n docker image inspect "$site_tag" --format '{{.Id}}')" == "$expected" ]] || fail 'website tag changed'
compose=(sudo -n docker compose --project-directory /opt/crypto-project -f "$base" -f "$override")
[[ "$("${compose[@]}" config --images crypto-project)" == "$site_tag" ]] || fail 'effective Compose image changed'
latest=$(sudo -n docker inspect --format '{{.Image}}|{{.Config.Image}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{index .Config.Labels "com.docker.compose.project.config_files"}}' crypto-project) || fail 'website disappeared before release'
[[ "$latest" == "$details" ]] || fail 'running website changed during preflight'
[[ "$(sudo -n docker image inspect "$site_tag" --format '{{.Id}}')" == "$expected" ]] || fail 'website tag changed during preflight'
sudo -n docker image tag "$candidate" "$site_tag"
"${compose[@]}" up -d --no-build --no-deps crypto-project
REMOTE_OVERLAY
  echo "Overlay applied; verify live health, routes and image identity separately."
  exit 0
fi

# The legacy full-tree sync does not load Jindan's Compose override and uses
# --delete. Confirm that the target lacks that override before any write.
if [[ ! "$REMOTE_DIR" =~ ^/[A-Za-z0-9._/-]+$ || "/$REMOTE_DIR/" == *"/../"* ]]; then
  echo "Cannot safely inspect the remote path; use a verified overlay release." >&2
  exit 2
fi
if ! override_status=$(ssh -o BatchMode=yes -o ConnectTimeout=12 "$REMOTE_HOST" bash -s -- "$REMOTE_DIR" <<'REMOTE_GUARD'
set -eu
root=$1
for directory in "$root" "$root/deploy" "$root/deploy/jindan-v1"; do
  # Symlinked or inaccessible paths cannot establish that the override is absent.
  if [ -L "$directory" ]; then exit 2; fi
  if [ -d "$directory" ]; then
    [ -r "$directory" ] && [ -x "$directory" ] || exit 2
  elif [ "$directory" = "$root" ] || [ -e "$directory" ]; then
    exit 2
  else
    printf 'absent\n'
    exit 0
  fi
done
override="$root/deploy/jindan-v1/web-override.yml"
if [ -e "$override" ] || [ -L "$override" ]; then
  printf 'present\n'
else
  printf 'absent\n'
fi
REMOTE_GUARD
); then
  echo "Cannot confirm the remote override state; use a verified overlay release." >&2
  exit 1
fi
if [[ "$override_status" != "absent" ]]; then
  echo "Jindan override exists or its state is uncertain; use a verified overlay release." >&2
  exit 1
fi

EXCLUDES=(
  "--exclude=.git/"
  "--exclude=.DS_Store"
  "--exclude=.env"
  "--exclude=.env.*"
  "--exclude=!.env.example"
  "--exclude=__pycache__/"
  "--exclude=.pycache/"
  "--exclude=.pycache-check/"
  "--exclude=*.pyc"
  "--exclude=.pytest_cache/"
  "--exclude=.venv/"
  "--exclude=venv/"
  "--exclude=node_modules/"
  "--exclude=output/"
  "--exclude=tmp/"
  "--exclude=dist/"
  "--exclude=.next/"
  "--exclude=.wrangler/"
  "--exclude=public/site/"
  "--exclude=reports/"
  "--exclude=backups/"
  "--exclude=*_report.html"
  "--exclude=project_signal_report_*.html"
  "--exclude=runtime/*"
  "--exclude=signal_events.jsonl"
  "--exclude=strategy_trades.json"
  "--exclude=binance_strategy_bot_state.json"
  "--exclude=binance_strategy_bot_events.jsonl"
  "--exclude=okx_demo_bot_state.json"
  "--exclude=okx_market_cap_bot_state.json"
  "--exclude=okx_market_cap_bot_events.jsonl"
  "--exclude=*.log"
  "--exclude=*.tmp"
  "--exclude=*.swp"
)

echo "Deploying local code to ${REMOTE_HOST}:${REMOTE_DIR}"
rsync -az --delete "${EXCLUDES[@]}" ./ "${REMOTE_HOST}:${REMOTE_DIR}/"

if [[ "${BUILD_ONLY}" == "yes" ]]; then
  echo "Building ${SERVICE} on ${REMOTE_HOST} without restarting containers"
  ssh "${REMOTE_HOST}" "cd ${REMOTE_DIR} && sudo -n docker compose ${COMPOSE_FILES} build ${SERVICE}"
  echo
  echo "Done. Built ${SERVICE} without restarting running containers."
  exit 0
fi

echo "Rebuilding and restarting ${SERVICE} on ${REMOTE_HOST}"
ssh "${REMOTE_HOST}" "cd ${REMOTE_DIR} && sudo -n docker compose ${COMPOSE_FILES} up -d --build ${SERVICE}"

echo "Checking health endpoint"
ssh "${REMOTE_HOST}" "for i in 1 2 3 4 5 6 7 8 9 10; do curl -fsS http://127.0.0.1/api/health && exit 0; sleep 2; done; exit 1"

echo
echo "Done. Public URL: http://119.28.142.113"
