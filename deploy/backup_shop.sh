#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

BACKUP_ROOT="${BACKUP_ROOT:-/opt/backups/telegram-sepay-shop/automated}"
KEY_FILE="${BACKUP_KEY_FILE:-/root/.config/telegram-shop/backup.pass}"
SOURCE_ROOT="${SHOP_SOURCE_ROOT:-/opt/telegram-sepay-shop}"
POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-telegram-sepay-shop-postgres-1}"
REDIS_CONTAINER="${REDIS_CONTAINER:-telegram-sepay-shop-redis-1}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
archive_name="telegram-shop-${timestamp}.tar.gz.enc"
final_archive="${BACKUP_ROOT}/${archive_name}"
work_dir=""
plain_archive=""
encrypted_tmp=""

mkdir -p "${BACKUP_ROOT}" "$(dirname "${KEY_FILE}")"
exec 9>/run/lock/telegram-shop-backup.lock
if ! flock -n 9; then
  echo "Another shop backup is already running" >&2
  exit 0
fi

if [[ ! -s "${KEY_FILE}" ]]; then
  echo "Backup encryption key is missing: ${KEY_FILE}" >&2
  exit 1
fi
if [[ ! -d "${SOURCE_ROOT}" ]]; then
  echo "Shop source directory is missing: ${SOURCE_ROOT}" >&2
  exit 1
fi

cleanup() {
  if [[ -n "${work_dir}" && -d "${work_dir}" ]]; then
    rm -rf -- "${work_dir}"
  fi
  if [[ -n "${plain_archive}" && -f "${plain_archive}" ]]; then
    rm -f -- "${plain_archive}"
  fi
  if [[ -n "${encrypted_tmp}" && -f "${encrypted_tmp}" ]]; then
    rm -f -- "${encrypted_tmp}"
  fi
  return 0
}
trap cleanup EXIT

work_dir="$(mktemp -d "${BACKUP_ROOT}/.work-${timestamp}-XXXXXX")"
plain_archive="$(mktemp "${BACKUP_ROOT}/.plain-${timestamp}-XXXXXX.tar.gz")"
encrypted_tmp="$(mktemp "${BACKUP_ROOT}/.encrypted-${timestamp}-XXXXXX")"

docker inspect "${POSTGRES_CONTAINER}" >/dev/null
docker inspect "${REDIS_CONTAINER}" >/dev/null

docker exec "${POSTGRES_CONTAINER}" \
  pg_dump -U shop --no-owner --no-privileges shop \
  | gzip -9 >"${work_dir}/postgres.sql.gz"
gzip -t "${work_dir}/postgres.sql.gz"

redis_tmp="/tmp/shop-backup-${timestamp}.rdb"
docker exec "${REDIS_CONTAINER}" redis-cli --rdb "${redis_tmp}" >/dev/null
docker cp "${REDIS_CONTAINER}:${redis_tmp}" "${work_dir}/redis.rdb" >/dev/null
docker exec "${REDIS_CONTAINER}" rm -f -- "${redis_tmp}"

tar \
  --exclude='./.git' \
  --exclude='./.pytest_cache' \
  --exclude='./.ruff_cache' \
  --exclude='*.tar.gz' \
  --exclude='*.tar.gz.enc' \
  -czf "${work_dir}/application.tar.gz" \
  -C "${SOURCE_ROOT}" .

config_files=()
for path in \
  /etc/caddy/Caddyfile \
  /etc/fail2ban/jail.d/sshd.local \
  /etc/ssh/sshd_config.d/99-hardening.conf \
  /etc/ufw/user.rules \
  /etc/ufw/user6.rules \
  "${SOURCE_ROOT}/.deployed-commit"; do
  [[ -e "${path}" ]] && config_files+=("${path}")
done
if (( ${#config_files[@]} > 0 )); then
  tar -czf "${work_dir}/system-config.tar.gz" "${config_files[@]}" 2>/dev/null
else
  tar -czf "${work_dir}/system-config.tar.gz" --files-from /dev/null
fi

cat >"${work_dir}/RESTORE.txt" <<'EOF'
1. Decrypt this archive with deploy/decrypt_backup.py and the offsite backup key.
2. Extract application.tar.gz into /opt/telegram-sepay-shop and restore .env permissions.
3. Start PostgreSQL and import postgres.sql.gz with psql before starting the app.
4. Restore system-config.tar.gz only after reviewing paths for the replacement server.
5. Redis is optional; redis.rdb contains transient bot/FSM state at backup time.
EOF

{
  echo "created_at_utc=${timestamp}"
  echo "hostname=$(hostname -f 2>/dev/null || hostname)"
  echo "deployed_commit=$(cat "${SOURCE_ROOT}/.deployed-commit" 2>/dev/null || echo unknown)"
  echo "postgres_size=$(docker exec "${POSTGRES_CONTAINER}" psql -U shop -d shop -Atc 'select pg_database_size(current_database())')"
  echo "docker_version=$(docker version --format '{{.Server.Version}}')"
} >"${work_dir}/metadata.txt"

(
  cd "${work_dir}"
  sha256sum \
    postgres.sql.gz \
    redis.rdb \
    application.tar.gz \
    system-config.tar.gz \
    metadata.txt \
    RESTORE.txt \
    >manifest.sha256
)

tar -czf "${plain_archive}" -C "${work_dir}" .
openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 -md sha256 \
  -pass "file:${KEY_FILE}" \
  -in "${plain_archive}" \
  -out "${encrypted_tmp}"

mv -f -- "${encrypted_tmp}" "${final_archive}"
encrypted_tmp=""
sha256sum "${final_archive}" >"${final_archive}.sha256"
ln -sfn "${archive_name}" "${BACKUP_ROOT}/latest.enc"
ln -sfn "${archive_name}.sha256" "${BACKUP_ROOT}/latest.enc.sha256"

find "${BACKUP_ROOT}" -maxdepth 1 -type f -name 'telegram-shop-*.tar.gz.enc' \
  -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_ROOT}" -maxdepth 1 -type f -name 'telegram-shop-*.tar.gz.enc.sha256' \
  -mtime "+${RETENTION_DAYS}" -delete

echo "Backup completed: ${final_archive}"
