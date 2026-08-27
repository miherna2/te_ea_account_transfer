$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($env:UV_PROJECT_ENVIRONMENT)) {
    $CacheRoot = if ($env:LOCALAPPDATA) {
        $env:LOCALAPPDATA
    } else {
        Join-Path $HOME ".cache"
    }
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $CacheRoot "te-ea-account-transfer\.venv"
}

$Timestamp = Get-Date -Format "HH:mm:ss"
Write-Host "$Timestamp INFO [launcher] using local environment $env:UV_PROJECT_ENVIRONMENT"
Write-Host "$Timestamp INFO [launcher] starting te-agent-migrate"

& uv run --project $ProjectDir te-agent-migrate @args
exit $LASTEXITCODE
