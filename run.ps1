$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MinimalDir = Join-Path $ProjectDir "TE_AGENT_MIGRATE_MINIMAL_REQUIREMENTS"
$Python = Get-Command python -CommandType Application -ErrorAction SilentlyContinue
if (-not $Python) {
    $Python = Get-Command python3 -CommandType Application -ErrorAction SilentlyContinue
}
$PythonLauncher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue

# Use the documented module directly; the legacy uv.lock describes a retired implementation.
Push-Location $MinimalDir
try {
    if ($Python) {
        & $Python.Source -m te_agent_migrate @args
    } elseif ($PythonLauncher) {
        & $PythonLauncher.Source -3 -m te_agent_migrate @args
    } else {
        Write-Error "Python 3.6 or newer is required. Install Python, then run this launcher again."
    }
    $Code = $LASTEXITCODE
} finally {
    Pop-Location
}

exit $Code
