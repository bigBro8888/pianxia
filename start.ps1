$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
Write-Host "片匣 starting at http://127.0.0.1:8765" -ForegroundColor Green
python -m uvicorn app:app --host 127.0.0.1 --port 8765
