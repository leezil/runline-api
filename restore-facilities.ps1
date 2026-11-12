param([Parameter(Mandatory=$true)][string]$BackupFile)
$ErrorActionPreference = 'Stop'
try {
    $pythonExe = (Get-Command python.exe -ErrorAction Stop).Source
    & $pythonExe (Join-Path $PSScriptRoot 'facility_backup.py') restore --file $BackupFile
    exit $LASTEXITCODE
} catch {
    Write-Host 'Restore could not start. Check Python installation and backup file.'
    exit 1
}
