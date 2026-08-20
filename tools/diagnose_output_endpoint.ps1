param([int]$StationId = 2)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$python = "C:\Users\tedu\AppData\Local\Programs\Python\Python312\python.exe"
$env:CLEANROOM_DB_PATH = "C:\ProgramData\RadioTEDU\OnAir\cleanroom.db"
$env:CLEANROOM_DATA_ROOT = "C:\ProgramData\RadioTEDU\OnAir"
$env:CLEANROOM_USER_CONFIG_ROOT = "C:\ProgramData\RadioTEDU\OnAir"
$env:CLEANROOM_JWT_SECRET_FILE = "C:\Users\tedu\AppData\Local\RadioTEDU\OnAir\secrets\jwt-signing.key"
$env:PYTHONPATH = $repoRoot
$stdoutPath = Join-Path $env:TEMP "radiotedu-8101-out.log"
$stderrPath = Join-Path $env:TEMP "radiotedu-8101-err.log"

$process = Start-Process `
    -FilePath $python `
    -ArgumentList @("-X", "utf8", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8101", "--ws", "none", "--lifespan", "off") `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

try {
    $deadline = (Get-Date).AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 300
        $listener = Get-NetTCPConnection -LocalPort 8101 -State Listen -ErrorAction SilentlyContinue
    } until ($listener -or $process.HasExited -or (Get-Date) -ge $deadline)

    & $python -X utf8 (Join-Path $repoRoot "tools\start_verified_broadcast.py") $StationId `
        --base-url "http://127.0.0.1:8101" --api-output-check
    $probeExit = $LASTEXITCODE
    if ($probeExit -ne 0) {
        Get-Content -LiteralPath $stderrPath -Tail 120 |
            Select-String -Pattern "Traceback|File |Error|Exception|KeyError|TypeError|OperationalError" |
            Select-Object -Last 30
    }
    exit $probeExit
}
finally {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
}
