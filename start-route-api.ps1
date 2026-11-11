$ErrorActionPreference = 'Stop'
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Python missing: $python" }
& $python -u (Join-Path $PSScriptRoot 'route-api-session.py')
exit $LASTEXITCODE
