param([int]$Port = 8088)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create Python environment' }
    & $taskPython -m pip install -r requirements.lock.txt
    if ($LASTEXITCODE -ne 0) { throw 'Cannot install dependencies' }
    & $taskPython -m pip install --no-deps -e .
    if ($LASTEXITCODE -ne 0) { throw 'Cannot install application' }
}
& $taskPython -m application serve --port $Port
