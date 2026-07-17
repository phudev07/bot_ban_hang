#!/bin/sh
set -eu

cd /opt/telegram-sepay-shop
umask 077

db_password="$(openssl rand -hex 32)"
printf "ALTER ROLE shop WITH PASSWORD '%s';\n" "$db_password" \
    | docker exec -i telegram-sepay-shop-postgres-1 psql -U shop -d postgres >/dev/null

temporary_env="$(mktemp)"
sed '/^POSTGRES_PASSWORD=/d;/^DATABASE_URL=/d' .env >"$temporary_env"
printf '\nPOSTGRES_PASSWORD=%s\n' "$db_password" >>"$temporary_env"
printf 'DATABASE_URL=postgresql+asyncpg://shop:%s@postgres:5432/shop\n' "$db_password" \
    >>"$temporary_env"
install -m 600 "$temporary_env" .env
rm -f "$temporary_env"
unset db_password

docker compose up -d --remove-orphans
