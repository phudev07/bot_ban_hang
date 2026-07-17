# Backup and disaster recovery

The production VPS creates an encrypted backup every four hours at approximately
03:30, 07:30, 11:30, 15:30, 19:30 and 23:30 Asia/Bangkok. A randomized delay of up
to 10 minutes avoids a fixed load spike. The Windows scheduled task pulls the newest
archive at 03:50, 07:50, 11:50, 15:50, 19:50 and 23:50, and retries when the PC
becomes available.

If Windows is off at a scheduled time, `StartWhenAvailable` runs the missed task
after the computer starts, the owner signs in and network access is available. The
task uses the owner's interactive Windows session, so it does not run while the
computer remains at the sign-in screen.

The recovery path was verified on 2026-07-18 by decrypting an offsite archive,
checking its internal manifest and importing its PostgreSQL dump into a temporary
database on the production server. The temporary database was removed afterward.

## What is included

- PostgreSQL SQL dump with users, balances, products, orders, deposits and API clients.
- Application source and the production `.env` file.
- Redis snapshot for transient Telegram/FSM state.
- Caddy, SSH and fail2ban configuration when present.
- SHA256 manifest for every file inside the archive.

## Automated jobs

- VPS timer: `telegram-shop-backup.timer`.
- VPS service: `telegram-shop-backup.service`.
- Windows task: `VietShare-Shop-Offsite-Backup`.

Check the next VPS run and the most recent Windows result:

```bash
systemctl list-timers telegram-shop-backup.timer
systemctl show telegram-shop-backup.service -p Result -p ExecMainStatus
```

```powershell
Get-ScheduledTaskInfo -TaskName "VietShare-Shop-Offsite-Backup"
Get-Content "$HOME\Documents\VietShareBackups\bot_ban_hang\last-success.txt"
```

## Install or reinstall the jobs

On the VPS, create the encryption key once. Never run this key-generation command
again unless intentionally invalidating every older backup:

```bash
install -d -m 700 /root/.config/telegram-shop
umask 077
openssl rand -base64 48 | tr -d '\n' > /root/.config/telegram-shop/backup.pass
```

Install the script and systemd units from the repository, then enable the timer:

```bash
install -m 700 deploy/backup_shop.sh /usr/local/sbin/telegram-shop-backup.sh
install -m 644 deploy/telegram-shop-backup.service /etc/systemd/system/
install -m 644 deploy/telegram-shop-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now telegram-shop-backup.timer
systemctl start telegram-shop-backup.service
```

Copy `backup.pass` securely to `C:\Users\DELL\.ssh\shop_backup.key`, preserving the
file bytes. On Windows, register the offsite pull task from the repository directory:

```powershell
$script = (Resolve-Path "deploy\pull_vps_backup.ps1").Path
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument `
  "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -Daily -At "03:50"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "VietShare-Shop-Offsite-Backup" `
  -Description "Pull and verify encrypted VietShare shop backup from VPS" `
  -Action $action -Trigger $trigger -Settings $settings -Force
$task = Get-ScheduledTask -TaskName "VietShare-Shop-Offsite-Backup"
$task.Triggers[0].Repetition.Interval = "PT4H"
$task.Triggers[0].Repetition.Duration = "P1D"
Set-ScheduledTask -InputObject $task
```

## Locations and retention

- VPS: `/opt/backups/telegram-sepay-shop/automated`, retained for 14 days.
- Windows: `C:\Users\DELL\Documents\VietShareBackups\bot_ban_hang`, retained for 90 days.
- Decryption key: `C:\Users\DELL\.ssh\shop_backup.key`.

Keep an additional offline copy of the decryption key. Losing both the VPS and this key
makes the encrypted backups unrecoverable.

Copy the key as a file without opening or re-saving it in a text editor. Store one copy
on an encrypted USB drive or another password-protected device that is not the VPS.
For 3-2-1 coverage, periodically copy both the key and the Windows backup folder to an
encrypted external drive, keeping the key separate from any publicly shared storage.

## Decrypt on Windows

```powershell
python deploy\decrypt_backup.py `
  "C:\Users\DELL\Documents\VietShareBackups\bot_ban_hang\telegram-shop-YYYYMMDDTHHMMSSZ.tar.gz.enc" `
  --key-file "C:\Users\DELL\.ssh\shop_backup.key" `
  --output "$env:TEMP\telegram-shop-restore.tar.gz"
```

Extract the resulting archive. Verify the included files:

```powershell
tar -xzf "$env:TEMP\telegram-shop-restore.tar.gz" -C "$env:TEMP\telegram-shop-restore"
Get-Content "$env:TEMP\telegram-shop-restore\manifest.sha256"
```

Run `Get-FileHash -Algorithm SHA256` for each listed payload and compare it with
`manifest.sha256` before starting a restore.

## Restore PostgreSQL on a replacement VPS

1. Install Docker and deploy the project files from `application.tar.gz`.
2. Copy the recovered `.env` file and make sure its database password matches Compose.
3. Start only PostgreSQL and Redis.
4. Create an empty `shop` database and import the dump.
5. Start the app and verify `/health`, the admin dashboard and Telegram polling.

Example import after copying `postgres.sql.gz` to the new VPS:

```bash
docker compose up -d postgres redis
gunzip -c postgres.sql.gz | docker compose exec -T postgres psql -U shop -d shop
docker compose up -d --build app
```

Do not restore `system-config.tar.gz` blindly on a different operating system. Review the
paths first, then install the Caddy and security configuration that applies to the new VPS.
