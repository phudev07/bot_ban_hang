# VietShare Shop - Deployment and VPS Runbook

Last reviewed: 2026-07-22

This file is the handoff document for a new Codex conversation or a new operator. Read
it before changing production. It intentionally contains no password, API key, bot token,
encryption key, database password, or backup decryption key.

## 0. Cach dung khi doi cuoc tro chuyen

1. Mo dung thu muc `C:\Users\DELL\Documents\toolcode\bot_ban_hang` trong Codex.
2. Gui doan ban giao tai muc 16, sau do mo ta viec can lam.
3. Yeu cau Codex doc het file nay, kiem tra Git va `.deployed-commit` truoc khi sua.
4. Neu xu ly mot don cu the, gui kem ma don shop, ma don nguon, log ID, user ID va
   thoi gian xay ra loi; khong gui API key hoac mat khau vao chat.

Tai lieu dung tieng Anh ky thuat de cau lenh va ten thanh phan khong bi hieu sai khi
ban giao, nhung moi thao tac production quan trong deu co lenh mau co the chay tu
PowerShell tren may Windows nay.

## 1. Golden rules

1. The local Git repository is the source of truth. Production is a deployed archive and
   is not a Git checkout.
2. Never commit `.env`, backup archives, database dumps, private SSH keys, API keys, or
   customer account data. The GitHub repository is public.
3. Before editing, run `git status --short`. Preserve any unrelated user changes.
4. Before deployment, run the full test suite, Ruff, and `git diff --check`.
5. Do not test a supplier purchase on production unless the owner explicitly approves a
   real purchase and the possible wallet debit.
6. Do not retry an ambiguous supplier purchase with a new idempotency key. Inspect the
   purchase-attempt and supplier-audit records first.
7. Back up before manual database repair or a risky migration.

## 2. Project locations

| Item | Location |
|---|---|
| Local repository | `C:\Users\DELL\Documents\toolcode\bot_ban_hang` |
| GitHub | `https://github.com/phudev07/bot_ban_hang.git` |
| Production branch | `main` |
| VPS | `160.191.243.91` |
| SSH login | `root@160.191.243.91` |
| Local SSH private key | `C:\Users\DELL\.ssh\codex_vps` |
| Application directory on VPS | `/opt/telegram-sepay-shop` |
| Deployment marker | `/opt/telegram-sepay-shop/.deployed-commit` |
| Production environment file | `/opt/telegram-sepay-shop/.env` |
| VPS encrypted backups | `/opt/backups/telegram-sepay-shop/automated` |
| Windows offsite backups | `C:\Users\DELL\Documents\VietShareBackups\bot_ban_hang` |
| Backup decryption key | `C:\Users\DELL\.ssh\shop_backup.key` |

Connect from PowerShell:

```powershell
ssh -i "$HOME\.ssh\codex_vps" root@160.191.243.91
```

## 3. Public services and domains

| Service | URL | Notes |
|---|---|---|
| Admin and Telegram webhook host | `https://160-191-243-91.sslip.io` | Admin is under `/admin` |
| Warehouse API | `https://token.vietshare.site/v1` | Cloudflare-proxied API domain |
| Warehouse API guide | `https://token.vietshare.site/docs` | Public integration documentation |
| Internal health check | `http://127.0.0.1:8080/health` | Only call from the VPS |

The sslip.io host deliberately returns `404` for `/v1` and `/docs`. The warehouse API
domain returns `403` when it is accessed directly instead of through Cloudflare. Other
paths on `token.vietshare.site` return `404`.

## 4. Runtime architecture

```text
Telegram users / SePay / API partners
                 |
          Cloudflare or Caddy
                 |
       127.0.0.1:8080 (app)
          /             \
 PostgreSQL 16        Redis 7
 persistent data     rate limits/FSM
```

Docker Compose services:

| Service | Container | Purpose |
|---|---|---|
| `app` | `telegram-sepay-shop-app-1` | Telegram polling, FastAPI, admin, webhook, workers |
| `postgres` | `telegram-sepay-shop-postgres-1` | Durable business data |
| `redis` | `telegram-sepay-shop-redis-1` | Rate limiting and transient state |

The app is limited to 768 MB RAM, uses a read-only filesystem, drops Linux capabilities,
and binds port 8080 only to localhost. Caddy is the public reverse proxy.

## 5. Important application behavior

- One multi-account purchase is one shop order (`batch_code`) with multiple order rows.
- Each inventory item and order row stores its actual supplier provider. Admin order pages
  must use this per-row value, not the product's primary fulfillment source; mixed batches
  can show both Sumi and Le Hai.
- Supplier cost is saved at delivery time. Dashboard profit is revenue minus cost minus
  referral commission.
- Sumi, Le Hai, and RentSim keys live only in production `.env`.
- Warehouse API partners receive shop product IDs and shop selling prices, never supplier
  keys, supplier URLs, supplier product IDs, supplier order IDs, or supplier cost.
- Warehouse API only sells active account products. SMS rental is excluded.
- Public API orders require HMAC, nonce, timestamp, idempotency key, and `max_unit_price`.
- Le Hai Jio 18M may resolve to the temporary sale ID `sale_link18mgemini`.
- If the temporary Le Hai sale purchase endpoint returns HTTP 5xx, the bot sets stock to
  zero and opens a 10-minute purchase circuit. Later customers are blocked locally and do
  not call Le Hai. The circuit is restored from recent failure logs after an app restart.
- The optional "notify stock without source top-up" switch has a per-product 10-minute
  notification cooldown and sends only the latest stock increase.
- Direct QR purchases that cannot be fulfilled are credited to the customer's wallet.

## 6. Standard local workflow

Open PowerShell in the repository:

```powershell
Set-Location "C:\Users\DELL\Documents\toolcode\bot_ban_hang"
git status --short
git branch --show-current
git log -5 --oneline
```

Install development dependencies when preparing a new machine:

```powershell
python -m pip install -e ".[dev]"
```

Run validation:

```powershell
python -m pytest -q
ruff check .
git diff --check
```

Use `python -m pytest`, not a random global `pytest` executable. This ensures the project
root is on Python's import path and avoids `ModuleNotFoundError: deploy`.

Commit and push only the intended files:

```powershell
git status --short
git add <files-you-changed>
git commit -m "Describe the production change"
git push origin main
```

Never use `git reset --hard` or overwrite unrelated local changes.

## 7. Deploy application code to the VPS

The VPS directory has no `.git`. Deployment uses `git archive`, preserving the production
`.env` because `.env` is not tracked by Git.

Run from the repository after tests and push succeed:

```powershell
$commit = (git rev-parse --short HEAD).Trim()
$archive = Join-Path $env:TEMP "bot_ban_hang-$commit.tar.gz"

git archive --format=tar.gz -o $archive HEAD
scp -q -i "$HOME\.ssh\codex_vps" $archive `
  "root@160.191.243.91:/tmp/bot_ban_hang-$commit.tar.gz"

$remoteScript = @"
set -e
cd /opt/telegram-sepay-shop
tar -xzf /tmp/bot_ban_hang-$commit.tar.gz -C /opt/telegram-sepay-shop
chmod 600 .env
docker compose up -d --build --wait app
curl -fsS http://127.0.0.1:8080/health
printf '%s\n' '$commit' > .deployed-commit
rm -f /tmp/bot_ban_hang-$commit.tar.gz
"@

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteScript))
ssh -i "$HOME\.ssh\codex_vps" root@160.191.243.91 `
  "echo $encoded | base64 -d | bash"

Remove-Item -LiteralPath $archive
```

This recreates only `app`; PostgreSQL and Redis remain running. Do not run
`docker compose down -v`, because `-v` deletes persistent volumes.

## 8. Verify a deployment

Check the marker, containers, internal health, and recent errors:

```powershell
$remoteScript = @'
set -e
cd /opt/telegram-sepay-shop
cat .deployed-commit
docker compose ps
curl -fsS http://127.0.0.1:8080/health
docker compose logs --since=10m app | tail -200
'@

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteScript))
ssh -i "$HOME\.ssh\codex_vps" root@160.191.243.91 `
  "echo $encoded | base64 -d | bash"
```

Expected state:

- `.deployed-commit` equals `git rev-parse --short HEAD`.
- `app`, `postgres`, and `redis` are `healthy`.
- `/health` returns `{"status":"ok"}`.
- Logs contain no repeating traceback, database connection loop, or restart loop.

Safe public checks:

```powershell
curl.exe -sS -o NUL -w "Warehouse health: %{http_code}`n" `
  "https://token.vietshare.site/v1/health"
curl.exe -sS -o NUL -w "API docs: %{http_code}`n" `
  "https://token.vietshare.site/docs"
curl.exe -sS -o NUL -w "Admin: %{http_code}`n" `
  "https://160-191-243-91.sslip.io/admin"
curl.exe -sS -o NUL -w "Blocked API on sslip.io: %{http_code}`n" `
  "https://160-191-243-91.sslip.io/v1"
```

Warehouse health and API docs should return `200`. Admin should return `303` to the login
page when there is no session (or `200` after following the redirect). The blocked
sslip.io API check should return `404`.

Do not perform a production purchase as a smoke test. Use read-only catalog, health, and
database checks unless the owner explicitly authorizes a real debit.

## 9. Roll back application code

Rollback also uses an archive. It does not require changing the local branch:

```powershell
$rollbackCommit = "PUT_KNOWN_GOOD_COMMIT_HERE"
$archive = Join-Path $env:TEMP "bot_ban_hang-$rollbackCommit.tar.gz"

git archive --format=tar.gz -o $archive $rollbackCommit
scp -q -i "$HOME\.ssh\codex_vps" $archive `
  "root@160.191.243.91:/tmp/bot_ban_hang-$rollbackCommit.tar.gz"

$remoteScript = @"
set -e
cd /opt/telegram-sepay-shop
tar -xzf /tmp/bot_ban_hang-$rollbackCommit.tar.gz -C /opt/telegram-sepay-shop
chmod 600 .env
docker compose up -d --build --wait app
curl -fsS http://127.0.0.1:8080/health
printf '%s\n' '$rollbackCommit' > .deployed-commit
rm -f /tmp/bot_ban_hang-$rollbackCommit.tar.gz
"@

$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($remoteScript))
ssh -i "$HOME\.ssh\codex_vps" root@160.191.243.91 `
  "echo $encoded | base64 -d | bash"

Remove-Item -LiteralPath $archive
```

After rollback, repeat every verification in section 8. A code rollback does not reverse
database data already written. Review migrations before rolling back across schema changes.

## 10. Production configuration and secrets

The template is `.env.example`; the real file is only on the VPS:

```bash
cd /opt/telegram-sepay-shop
stat -c '%a %U:%G %n' .env
```

Expected permission is `600 root:root`. Never print the complete file into a chat or log.
When an environment-only value changes, recreate the app:

```bash
cd /opt/telegram-sepay-shop
docker compose up -d --force-recreate app
docker compose ps
```

Rotate the Le Hai key without putting it in shell history. Copy the new key to the Windows
clipboard, then pipe it through SSH:

```powershell
Get-Clipboard | ssh -i "$HOME\.ssh\codex_vps" root@160.191.243.91 `
  "cd /opt/telegram-sepay-shop && python deploy/set_lehai_key.py .env"

ssh -i "$HOME\.ssh\codex_vps" root@160.191.243.91 `
  "cd /opt/telegram-sepay-shop && docker compose up -d --force-recreate app"
```

After any key rotation, check app logs and use only balance/catalog endpoints. Do not place
a real supplier order merely to verify a key.

## 11. Caddy and Cloudflare

Repository configuration: `deploy/Caddyfile`.

Production configuration: `/etc/caddy/Caddyfile`.

Only deploy Caddy when the domain, routing, request-size limit, or security headers change:

```powershell
scp -q -i "$HOME\.ssh\codex_vps" deploy\Caddyfile `
  root@160.191.243.91:/tmp/telegram-shop-Caddyfile

ssh -i "$HOME\.ssh\codex_vps" root@160.191.243.91 `
  "caddy validate --config /tmp/telegram-shop-Caddyfile && install -m 644 /tmp/telegram-shop-Caddyfile /etc/caddy/Caddyfile && systemctl reload caddy && systemctl is-active caddy"
```

Cloudflare DNS for `token.vietshare.site` must remain proxied. If it becomes DNS-only,
direct-origin protection will return `403` to legitimate API users.

## 12. Database inspection

Open PostgreSQL inside its container:

```bash
cd /opt/telegram-sepay-shop
docker exec -it telegram-sepay-shop-postgres-1 psql -U shop -d shop
```

Useful read-only commands:

```sql
SELECT now();
SELECT COUNT(*) FROM users;
SELECT COUNT(*) FROM orders;
SELECT COUNT(*) FROM deposits;
SELECT COUNT(*) FROM api_clients WHERE active IS TRUE;
SELECT id, batch_code, supplier_order_code, status, created_at
FROM orders
ORDER BY id DESC
LIMIT 20;
```

Inspect supplier purchase failures without exposing delivered account secrets:

```sql
SELECT id, provider, request_key, supplier_product_id, quantity,
       status, error_code, error_detail, supplier_order_code, completed_at
FROM supplier_purchase_attempts
ORDER BY id DESC
LIMIT 50;
```

Inspect supplier balance reconciliation:

```sql
SELECT id, provider, kind, amount, balance_before, balance_after,
       supplier_order_code, shop_order_code, created_at
FROM supplier_balance_transactions
ORDER BY id DESC
LIMIT 100;
```

Before a manual write:

1. Create or confirm a current encrypted backup.
2. Record the exact rows before the change.
3. Use a transaction and a narrow `WHERE` condition.
4. Re-read the changed rows and related wallet/order ledgers.
5. Never delete a suspicious transaction merely to hide an unresolved discrepancy.

## 13. Logs and performance

```bash
cd /opt/telegram-sepay-shop
docker compose logs --since=30m app
docker compose logs --since=30m postgres
docker stats --no-stream
free -h
df -h /
```

For a `502` on admin:

1. Run `docker compose ps`.
2. Call `curl -fsS http://127.0.0.1:8080/health` on the VPS.
3. Read `docker compose logs --since=15m app`.
4. Check `systemctl status caddy --no-pager` and `journalctl -u caddy --since=-15m`.
5. Restart only the failed layer. Prefer `docker compose restart app` over restarting every
   container.

For a slow admin or bot:

- Check database pool timeout warnings and PostgreSQL health.
- Check Redis health and rate-limit errors.
- Check supplier API timeouts separately from local response time.
- Check RAM and swap. The VPS currently has limited RAM, so avoid extra services.
- Do not remove indexes or increase worker concurrency without load testing.

## 14. Supplier incident checklist

When a supplier order fails:

1. Find the shop order/deposit and `supplier_purchase_attempts` row.
2. Confirm whether a supplier order code exists.
3. Compare supplier balance before and after.
4. Check whether the user wallet was deducted, credited, or refunded.
5. Reuse the same idempotency key if the code explicitly supports a safe retry.
6. Do not make a second purchase with a new key while the first result is ambiguous.

Le Hai Jio sale notes:

- Catalog canonical ID: `cdk_ggpro_18m`.
- Temporary sale alias: `sale_link18mgemini`.
- The provider has previously listed the alias but returned HTTP 500 from purchase.
- The local 10-minute circuit breaker prevents request flooding after the first failure.
- A direct payment that cannot be delivered falls back to the customer's wallet.

RentSim notes:

- SMS rentals are wallet-only.
- Each rental is reconciled independently by provider order ID.
- Provider-confirmed no-OTP/timeout rentals are refunded individually.
- RentSim is excluded from the warehouse API.

## 15. Backup operations

The VPS creates an encrypted backup every four hours using:

- Timer: `telegram-shop-backup.timer`.
- Service: `telegram-shop-backup.service`.
- Script: `/usr/local/sbin/telegram-shop-backup.sh`.

Check it:

```bash
systemctl list-timers telegram-shop-backup.timer --no-pager
systemctl show telegram-shop-backup.service -p Result -p ExecMainStatus
ls -lh /opt/backups/telegram-sepay-shop/automated | tail
```

The Windows scheduled task is `VietShare-Shop-Offsite-Backup`. It downloads the newest
encrypted archive when the PC becomes available:

```powershell
Get-ScheduledTaskInfo -TaskName "VietShare-Shop-Offsite-Backup"
Get-Content "$HOME\Documents\VietShareBackups\bot_ban_hang\last-success.txt"
```

Full restore instructions are in `deploy/BACKUP_RECOVERY.md`. Do not regenerate the backup
encryption key unless intentionally invalidating access to all older backups.

## 16. New conversation handoff

In a new Codex conversation, start with this instruction:

```text
Repository: C:\Users\DELL\Documents\toolcode\bot_ban_hang
Read DEPLOYMENT_RUNBOOK.md completely before making changes.
Check git status, the latest commits, and /opt/telegram-sepay-shop/.deployed-commit.
Preserve unrelated local changes and production .env.
Run python -m pytest -q, ruff check ., and git diff --check before deployment.
Production VPS is root@160.191.243.91 using C:\Users\DELL\.ssh\codex_vps.
Do not make a real supplier purchase unless I explicitly approve it.
```

Then provide the new task and any relevant shop order code, supplier order code, Telegram
user ID, deposit code, log ID, screenshot, or exact time window.

## 17. Files to read for specific work

| Work area | Main files |
|---|---|
| Product purchase and wallet flow | `app/services.py`, `app/wallet_ledger.py` |
| Sumi integration | `app/suppliers.py`, `app/supplier_recovery.py` |
| Le Hai integration | `app/lehai_suppliers.py` |
| RentSim/SMS | `app/rentsim.py`, `app/sms_rentals.py` |
| Telegram handlers | `app/handlers.py`, `app/keyboards.py` |
| Admin dashboard | `app/dashboard.py`, `app/templates/` |
| Broadcasts and stock/sale alerts | `app/broadcasts.py`, `app/stock_alerts.py`, `app/price_alerts.py` |
| Warehouse API | `app/public_api.py`, `app/partner_services.py` |
| Startup and schema migration | `app/main.py` |
| Runtime configuration | `app/config.py`, `.env.example` |
| Reverse proxy | `deploy/Caddyfile` |
| Backup and restore | `deploy/BACKUP_RECOVERY.md`, `deploy/backup_shop.sh` |

## 18. Deferred work: Sumi login-warranty tickets

Status reviewed on 2026-07-22. This is a saved investigation only. Do not implement or
change production until the owner explicitly asks to continue this work.

Current findings:

- The public Sumi warehouse guide exposes only product list/detail, wallet balance,
  order list/detail, and purchase endpoints.
- No public endpoint exists for creating a support ticket, submitting a warranty claim,
  checking claim status, replacing an account, or receiving a warranty refund.
- Sumi returns the warranty policy only inside each product's `description` field.
- `SP-GEF55PBV` and `SP-JMYJL2PL` currently state a one-hour login warranty. The Plus
  description also says no refund/return, so the intended scope is login failure only.
- The Sumi website advertises order lookup and support in one interface, but it returned
  HTTP 503 maintenance responses during the review. The authenticated ticket workflow and
  any private web endpoint could not be verified.
- The shop currently has no warranty-claim model, customer warranty button, admin ticket
  queue, or automated Sumi ticket submission.

Recommended future implementation if Sumi still has no supported ticket API:

1. Add a claim per delivered account/order row, not one claim for an entire multi-account
   batch. Enforce a database uniqueness constraint so concurrent taps cannot create two
   claims for the same account.
2. Show `Bao hanh dang nhap` only for Sumi orders and accept it only within one hour of
   `delivered_at`. Allow login failures only; reject usage bans, changed credentials,
   customer mistakes, and claims outside the supplier policy.
3. Store a claim code, user, shop order, Sumi `API-TELE-...` order code, affected order
   row, reason, status, timestamps, and an audit trail. Never expose another account from
   the same batch in the ticket.
4. Send the admin a structured ticket with buttons for `Da gui Sumi`, `Chap nhan`,
   `Tu choi`, and `Tra tai khoan thay the`. The admin manually contacts Sumi while no
   supplier endpoint exists.
5. Encrypt replacement account data, deliver it once through the existing delivery path,
   retain both old and replacement audit references, and prevent duplicate replacement or
   wallet credit under concurrent admin actions.
6. Add an admin warranty dashboard with pending/approved/replaced/rejected/expired tabs,
   search by claim code, shop order, source order, Telegram ID, and username.

Before implementation, recheck the Sumi website after maintenance. If submitting a real
claim creates a documented and stable API request, ask Sumi for permission and official
API details before integrating it. Do not automate a logged-in browser session or use a
Telegram userbot as the production warranty transport.
