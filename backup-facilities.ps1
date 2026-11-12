param()
$ErrorActionPreference = 'Stop'
try {
    $pythonExe = (Get-Command python.exe -ErrorAction Stop).Source
    & $pythonExe (Join-Path $PSScriptRoot 'facility_backup.py') backup
    exit $LASTEXITCODE
} catch {
    Write-Host 'Backup could not start. Check Python installation.'
    exit 1
}
