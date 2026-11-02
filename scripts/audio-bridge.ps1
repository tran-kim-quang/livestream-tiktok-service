$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $repoRoot ".venv-bridge"
$pythonPath = Join-Path $venvPath "Scripts\python.exe"
$installedMarker = Join-Path $venvPath ".bridge-requirements-installed"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Host "[audio-bridge] Tạo môi trường Python .venv-bridge..."
    python -m venv $venvPath
}

if (-not (Test-Path -LiteralPath $installedMarker)) {
    Write-Host "[audio-bridge] Cài dependencies lần đầu..."
    & $pythonPath -m pip install -r (Join-Path $repoRoot "requirements-bridge.txt")
    New-Item -ItemType File -Path $installedMarker -Force | Out-Null
}

Push-Location $repoRoot
try {
    & $pythonPath -m app.audio.bridge @args
    $bridgeExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $bridgeExitCode
