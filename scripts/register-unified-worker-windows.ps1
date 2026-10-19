[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string]$RuntimeRoot = (Join-Path $PSScriptRoot '..'),
  [string]$TaskName = 'MediaWorker',
  [switch]$ReplaceExisting
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path -LiteralPath $RuntimeRoot).Path
$Worker = Join-Path $Root '.venv-unified\Scripts\ClapClipWoker.exe'
if (-not (Test-Path -LiteralPath $Worker)) { throw 'Install the unified Python 3.13 environment and ClapClipWoker.exe launcher first.' }
Push-Location $Root
try {
  $Runtime = & $Worker -B -c "import json,sys; from pathlib import Path; import onnxruntime as ort; assert sys.version_info[:2] == (3,13); assert 'DmlExecutionProvider' in ort.get_available_providers(); print(json.dumps({'gui':str(Path(sys._base_executable).with_name('pythonw.exe'))}))"
  if ($LASTEXITCODE -ne 0) { throw 'Unified Python 3.13 / DirectML preflight failed.' }
  $GuiPython = ($Runtime | ConvertFrom-Json).gui
} finally { Pop-Location }
if (-not (Test-Path -LiteralPath $GuiPython)) { throw 'GUI Python executable is missing.' }
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
  if (-not $ReplaceExisting) { throw "Task '$TaskName' exists. Drain and stop the old runtime before using -ReplaceExisting." }
  if (@($Existing.Actions).Count -ne 1 -or $Existing.Actions[0].Arguments -notmatch '^-m mediaworker\.supervisor --mode run(?:\s|$)') {
    throw 'Existing task is not the expected Worker supervisor; refusing replacement.'
  }
  if ($Existing.State -eq 'Running') { throw 'Stop the scheduled supervisor before replacement.' }
}
$User = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Shell = (Get-Process -Id $PID).Path
$Arguments = "-m mediaworker.supervisor --mode run --worker-exe `"$Worker`" --shell `"$Shell`" --all-capabilities"
$Action = New-ScheduledTaskAction -Execute $GuiPython -Argument $Arguments -WorkingDirectory $Root
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
  -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -StartWhenAvailable `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
if ($PSCmdlet.ShouldProcess($TaskName, 'Register one independent background Worker for all configured capabilities (no startup trigger)')) {
  Register-ScheduledTask -TaskName $TaskName -Action $Action -Principal $Principal -Settings $Settings `
    -Description 'One DirectML runtime: faceswap, cutout, enhance and download. Shared three slots; one download poller.' -Force:$ReplaceExisting | Out-Null
  Write-Output "Registered '$TaskName'. Start it with Start-ScheduledTask -TaskName '$TaskName'."
}
