$ErrorActionPreference = "Stop"
& .\.venv\Scripts\python.exe -m te_agent_migrate @args
exit $LASTEXITCODE
