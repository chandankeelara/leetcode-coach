$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
. .\.venv\Scripts\Activate.ps1
uvicorn coach.web.app:app --host 127.0.0.1 --port 8000
