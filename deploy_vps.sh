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
