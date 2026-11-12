param([switch]$DryRun)
$ErrorActionPreference = 'Stop'
try {
    $pythonExe = (Get-Command python.exe -ErrorAction Stop).Source
    $jobPath = Join-Path $PSScriptRoot 'facility_refresh_job.py'
    $arguments = @($jobPath)
    if ($DryRun) { $arguments += '--dry-run' }
    & $pythonExe @arguments
    exit $LASTEXITCODE
} catch {
    Write-Host 'Refresh could not start. Check Python installation and the project location.'
    exit 1
}
