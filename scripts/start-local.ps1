param([int]$Port = 8765, [string]$EnvFile = "")
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Local environment missing. Follow docs/local-mode.md to create .venv first.'
}
$arguments = @('-m', 'mediaworker', 'local', '--port', "$Port")
if ($EnvFile) { $arguments += @('--env-file', (Resolve-Path -LiteralPath $EnvFile).Path) }
Push-Location $root
try { & $python @arguments; if ($LASTEXITCODE) { exit $LASTEXITCODE } }
finally { Pop-Location }
