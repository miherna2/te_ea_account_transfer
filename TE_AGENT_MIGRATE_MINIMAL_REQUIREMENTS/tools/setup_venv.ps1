param(
    [string]$PythonBin = "python"
)

$ErrorActionPreference = "Stop"
& $PythonBin -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -c "import ssl, sys; print('Python runtime: %s' % sys.version.split()[0]); print('SSL runtime: %s' % getattr(ssl, 'OPENSSL_VERSION', 'unknown')); print('WARNING: This Python SSL runtime does not report TLS 1.3 support. Agents that require TLS 1.3 will reject UI connections. Recreate the environment with a TLS 1.3-capable Python interpreter.') if not getattr(ssl, 'HAS_TLSv1_3', False) else None"

Write-Host ""
Write-Host "Environment ready. Activate it with:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
