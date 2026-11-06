param([Parameter(ValueFromRemainingArguments=$true)][string[]]$SetupArgs)
$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'setup-worker.py'
if ($env:PYTHON) {
    & $env:PYTHON $script @SetupArgs
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.13 $script @SetupArgs
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python $script @SetupArgs
} else {
    throw 'Install CPython 3.13 x64 from https://www.python.org/downloads/; see docs/portable-setup.md.'
}
exit $LASTEXITCODE
