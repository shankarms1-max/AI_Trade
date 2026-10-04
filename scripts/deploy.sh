#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env.production}"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.prod.yml"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Copy .env.production.example and configure it first." >&2
  exit 2
fi

if [[ "${1:-}" == "--pull" ]]; then
  git -C "${ROOT_DIR}" pull --ff-only
elif [[ -n "${1:-}" ]]; then
  echo "Usage: bash scripts/deploy.sh [--pull]" >&2
  exit 2
fi

cd "${ROOT_DIR}"
COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}")

"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" build
"${COMPOSE[@]}" up -d --remove-orphans
"${COMPOSE[@]}" ps

echo "Deployment started. The migrate service must exit successfully before backend and collector start."
echo "Run: python scripts/deployment_smoke_test.py"
