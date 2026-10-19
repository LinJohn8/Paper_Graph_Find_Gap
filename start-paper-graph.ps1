param([switch]$InstallDependencies, [switch]$Production)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$backend = Join-Path $root 'research-self'
$frontend = Join-Path $root 'acceptodds'

if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw 'Node.js is required.' }
$nodeVersion = [version]((node --version).TrimStart('v'))
if ($nodeVersion -lt [version]'20.9.0') { throw 'Node.js 20.9 or newer is required.' }

if (-not (Test-Path (Join-Path $backend '.venv\Scripts\python.exe'))) {
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3.11 -m venv (Join-Path $backend '.venv') }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { python -m venv (Join-Path $backend '.venv') }
    else { throw 'Python 3.11+ is required.' }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python 3.11 virtual environment.' }
    $InstallDependencies = $true
}
$python = Join-Path $backend '.venv\Scripts\python.exe'
if ($InstallDependencies) {
    & $python -m pip install -e "${backend}[test]"
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
}
& $python -c 'import fastapi,uvicorn,httpx,yaml,dotenv; import sys; assert sys.version_info >= (3,11)'
if ($LASTEXITCODE -ne 0) { throw 'Python dependencies missing. Run with -InstallDependencies.' }
if ($InstallDependencies -or -not (Test-Path (Join-Path $frontend 'node_modules\next'))) {
    Push-Location $frontend
    npm ci
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw 'npm ci failed.' }
    Pop-Location
}

foreach ($port in @(8000,3000)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { throw "Port $port is already occupied. Stop the existing service first." }
}
$logs = Join-Path $root 'Data\logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$mode = if ($Production) { 'start' } else { 'dev' }
if ($Production -and -not (Test-Path (Join-Path $frontend '.next\BUILD_ID'))) { throw 'Run npm run build in acceptodds before -Production.' }
$api = Start-Process -FilePath $python -ArgumentList '-m','uvicorn','paper_ingest.api:app','--host','127.0.0.1','--port','8000' -WorkingDirectory $backend -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'api.stdout.log') -RedirectStandardError (Join-Path $logs 'api.stderr.log') -PassThru
$web = Start-Process -FilePath (Get-Command node).Source -ArgumentList 'node_modules/next/dist/bin/next',$mode,'--hostname','127.0.0.1','--port','3000' -WorkingDirectory $frontend -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'web.stdout.log') -RedirectStandardError (Join-Path $logs 'web.stderr.log') -PassThru
@{api=$api.Id;web=$web.Id} | ConvertTo-Json | Set-Content (Join-Path $logs 'processes.json')
foreach ($url in @('http://127.0.0.1:8000/api/health','http://127.0.0.1:3000')) {
    $ready = $false
    for ($attempt=0; $attempt -lt 30; $attempt++) {
        try { $response=Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 3; if ($response.StatusCode -eq 200) { $ready=$true; break } } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw "Startup failed: $url. Inspect Data\logs." }
}
Write-Output "Paper API PID $($api.Id): http://127.0.0.1:8000/docs"
Write-Output "Paper Graph PID $($web.Id): http://127.0.0.1:3000"
Write-Output "Stop: Stop-Process -Id $($api.Id),$($web.Id)"
