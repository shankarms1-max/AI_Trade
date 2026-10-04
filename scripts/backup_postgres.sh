#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env.production}"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.prod.yml"
BACKUP_DIR="${BACKUP_DIR:-${ROOT_DIR}/backups}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}." >&2
  exit 2
fi
ENV_RETENTION="$(awk -F= '$1 == "BACKUP_RETENTION_DAYS" {print substr($0, index($0, "=") + 1)}' "${ENV_FILE}" | tail -n 1 | tr -d '[:space:]')"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-${ENV_RETENTION:-14}}"
if ! [[ "${RETENTION_DAYS}" =~ ^[0-9]+$ ]]; then
  echo "BACKUP_RETENTION_DAYS must be a non-negative integer." >&2
  exit 2
fi

mkdir -p "${BACKUP_DIR}"
ROOT_REAL="$(realpath "${ROOT_DIR}")"
BACKUP_REAL="$(realpath "${BACKUP_DIR}")"
case "${BACKUP_REAL}" in
  "${ROOT_REAL}"/*) ;;
  *) echo "BACKUP_DIR must be inside the project directory." >&2; exit 2 ;;
esac

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_REAL}/nifty_research_${STAMP}.dump"
TEMP="${TARGET}.tmp"
COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}")

"${COMPOSE[@]}" exec -T postgres sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" "$POSTGRES_DB"' \
  > "${TEMP}"
test -s "${TEMP}"
mv "${TEMP}" "${TARGET}"
chmod 600 "${TARGET}"

find "${BACKUP_REAL}" -type f -name 'nifty_research_*.dump' -mtime "+${RETENTION_DAYS}" -delete
echo "Backup created: ${TARGET}"
