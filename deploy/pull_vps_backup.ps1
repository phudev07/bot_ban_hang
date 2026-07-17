param(
    [string]$VpsHost = "root@160.191.243.91",
    [string]$SshKey = "$HOME\.ssh\codex_vps",
    [string]$RemoteDirectory = "/opt/backups/telegram-sepay-shop/automated",
    [string]$Destination = "$HOME\Documents\VietShareBackups\bot_ban_hang",
    [int]$RetentionDays = 90
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $Destination | Out-Null

$remoteFile = (& ssh -i $SshKey $VpsHost "ls -1t '$RemoteDirectory'/telegram-shop-*.tar.gz.enc 2>/dev/null | head -n 1").Trim()
if (-not $remoteFile) {
    throw "VPS does not have an automated shop backup yet."
}

$fileName = [IO.Path]::GetFileName($remoteFile)
if ($fileName -notmatch '^telegram-shop-\d{8}T\d{6}Z\.tar\.gz\.enc$') {
    throw "VPS returned an unexpected backup filename."
}

$localFile = Join-Path $Destination $fileName
$checksumFile = "$localFile.sha256"
$partialFile = "$localFile.partial"
$partialChecksum = "$checksumFile.partial"

& scp -q -i $SshKey "${VpsHost}:${remoteFile}" $partialFile
if ($LASTEXITCODE -ne 0) { throw "Could not download the encrypted backup." }
& scp -q -i $SshKey "${VpsHost}:${remoteFile}.sha256" $partialChecksum
if ($LASTEXITCODE -ne 0) { throw "Could not download the backup checksum." }

$expectedHash = ((Get-Content -Raw -LiteralPath $partialChecksum).Trim() -split '\s+')[0].ToLowerInvariant()
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $partialFile).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) {
    throw "Backup checksum verification failed."
}

Move-Item -Force -LiteralPath $partialFile -Destination $localFile
Move-Item -Force -LiteralPath $partialChecksum -Destination $checksumFile

$cutoff = (Get-Date).AddDays(-[Math]::Max(7, $RetentionDays))
Get-ChildItem -LiteralPath $Destination -File -Filter 'telegram-shop-*.tar.gz.enc*' |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    Remove-Item -Force

$status = @(
    "last_success=$(Get-Date -Format o)"
    "backup=$localFile"
    "sha256=$actualHash"
) -join [Environment]::NewLine
[IO.File]::WriteAllText((Join-Path $Destination "last-success.txt"), $status)
Write-Output "Offsite backup ready: $localFile"
