param(
    [int]$Port = 8089,
    [decimal]$MaxSetupCostUsd = 2
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw 'Run the installation commands from README.md first.'
}
if ([string]::IsNullOrWhiteSpace($env:OPENROUTER_API_KEY)) {
    $taskSecureKey = Read-Host 'OpenRouter API key (hidden input)' -AsSecureString
    $env:OPENROUTER_API_KEY = [System.Net.NetworkCredential]::new('', $taskSecureKey).Password
}
if (-not (Test-Path -LiteralPath 'config/openrouter.json')) {
    Write-Host 'Running up to four paid contract probes using public synthetic data.'
    Write-Host ('Application reservation budget: USD ' + $MaxSetupCostUsd + '. No automatic retries. Set a spending limit on your OpenRouter key too.')
    & $taskPython -m application openrouter-setup --max-cost-usd $MaxSetupCostUsd
    if ($LASTEXITCODE -ne 0) { throw 'OpenRouter setup failed. The live configuration was not enabled; see the qualification report.' }
}
Write-Host 'Starting real-model public-data pilot. Human release is required; domain quality is not yet evaluated.'
& $taskPython -m application --root var/openrouter --config config/openrouter.json serve --port $Port
