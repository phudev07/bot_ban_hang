$ErrorActionPreference = "Stop"

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $PSScriptRoot "CodexVietShareSetup.cs"
$output = Join-Path $repositoryRoot "app\static\VietShare-Codex-Claude-Setup.exe"
$archive = Join-Path $repositoryRoot "app\static\VietShare-Codex-Claude-Setup.zip"
$compilerCandidates = @(
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
)
$compiler = $compilerCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $compiler) {
    throw "Khong tim thay trinh bien dich .NET Framework csc.exe."
}

& $compiler /nologo /target:winexe /optimize+ `
    /reference:System.dll `
    /reference:System.Drawing.dll `
    /reference:System.Windows.Forms.dll `
    /out:$output $source

if ($LASTEXITCODE -ne 0) {
    throw "Bien dich cong cu Codex va Claude that bai."
}

Compress-Archive -LiteralPath $output -DestinationPath $archive -CompressionLevel Optimal -Force

Write-Output "Built: $output"
Write-Output "Packed: $archive"
