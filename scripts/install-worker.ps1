# No arguments; Windows requests normal administrator consent for boot setup.
$ErrorActionPreference = 'Stop'
if ($args.Count) { throw 'Run this installer without arguments.' }
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -or -not [Environment]::Is64BitProcess) { throw 'Use native Windows x64 PowerShell.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $quoted = '"' + $PSCommandPath.Replace('"', '""') + '"'
    $child = Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @('-NoProfile','-File',$quoted) -PassThru -Wait
    exit $child.ExitCode
}
$root = Join-Path ([Environment]::GetFolderPath('CommonApplicationData')) 'ClapClipWorker'
$bootstrapMutex = [Threading.Mutex]::new($false, 'Global\ClapClipWorkerBootstrap-v1')
try { $acquired = $bootstrapMutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $acquired = $true }
if (-not $acquired) { throw 'Installer already running.' }
# The OS releases this mutex on process exit, including interrupted installs.
$marker = Join-Path $root 'bootstrap-owner.txt'
if (Test-Path -LiteralPath $root) {
    if ((Get-Item -LiteralPath $root).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unsafe host directory.' }
    if (-not (Test-Path -LiteralPath $marker) -or (Get-Content -LiteralPath $marker -Raw).Trim() -ne 'clapclip-bootstrap-v1') { throw 'Existing host directory needs ownership review.' }
    $owner = (Get-Acl -LiteralPath $root).GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin @('S-1-5-18','S-1-5-32-544',$identity.User.Value)) { throw 'Existing host owner needs review.' }
    $unsafe = (Get-Acl -LiteralPath $root).Access | Where-Object { $_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin @('S-1-5-18','S-1-5-32-544',$identity.User.Value,'S-1-5-19') -and ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::Write) }
    if ($unsafe) { throw 'Existing host permissions need review.' }
} else {
    New-Item -ItemType Directory -Path $root | Out-Null
    & icacls.exe $root /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-19:(OI)(CI)RX' | Out-Null
    if ($LASTEXITCODE) { throw 'Host directory protection failed.' }
    Set-Content -LiteralPath $marker -Value 'clapclip-bootstrap-v1' -Encoding ascii
}
$legacy = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'mediaworker (run|local|download-worker)|mediaworker.supervisor|ClapClipWoker' }
if ($legacy) { throw 'Existing Worker requires reuse or controlled migration; nothing was started.' }
$mamba = Join-Path $root 'micromamba.exe'
if (-not (Test-Path -LiteralPath $mamba)) {
    $temp = Join-Path $root ('micromamba.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/mamba-org/micromamba-releases/releases/download/2.10.0-0/micromamba-win-64.exe' -OutFile $temp
        if ((Get-FileHash -LiteralPath $temp -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'f8b4284bd4eb1134122eaf93b3ea6c7be567e596de5829e1a6f478e34e41974a') { throw 'Tool bootstrap integrity check failed.' }
        Move-Item -LiteralPath $temp -Destination $mamba
    } finally { if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp } }
}
if ((Get-FileHash -LiteralPath $mamba -Algorithm SHA256).Hash.ToLowerInvariant() -ne 'f8b4284bd4eb1134122eaf93b3ea6c7be567e596de5829e1a6f478e34e41974a') { throw 'Existing bootstrap integrity check failed.' }
$env:MAMBA_ROOT_PREFIX = Join-Path $root 'package-cache'
$tools = Join-Path $root 'tools'
if (-not (Test-Path -LiteralPath (Join-Path $tools 'conda-meta/history'))) {
    & $mamba --no-rc create --yes --prefix $tools --override-channels -c conda-forge 'python=3.13' 'nodejs=22' 'ffmpeg=*=gpl_*' git pip
    if ($LASTEXITCODE) { throw 'Native tool installation failed.' }
}
$env:PATH = "$tools;$tools\Scripts;$tools\Library\bin;$tools\Library\usr\bin;" + $env:PATH
$env:PYTHONNOUSERSITE = '1'; $env:MEDIAWORKER_LOAD_DOTENV = '0'; $env:PYTHONDONTWRITEBYTECODE = '1'
Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' -or $_.Name -in @('PYTHONPATH','PYTHONHOME') } | ForEach-Object { Remove-Item -LiteralPath ('Env:' + $_.Name) }
$env:GIT_CONFIG_NOSYSTEM = '1'; $env:GIT_CONFIG_GLOBAL = 'NUL'; $env:GIT_TERMINAL_PROMPT = '0'
$source = Join-Path $root 'installer-source'
$git = Join-Path $tools 'Library/bin/git.exe'
if (-not (Test-Path -LiteralPath $git)) { $git = Join-Path $tools 'Library/usr/bin/git.exe' }
if (-not (Test-Path -LiteralPath $source)) {
    & $git -c core.hooksPath=NUL -c http.followRedirects=false clone --single-branch --branch main --no-tags https://github.com/ClapClip/clapclip-worker.git $source
    if ($LASTEXITCODE) { throw 'Official installer download failed.' }
}
$origin = & $git -C $source config --get remote.origin.url
if ($LASTEXITCODE -or $origin -ne 'https://github.com/ClapClip/clapclip-worker.git') { throw 'Unexpected installer origin.' }
$dirty = & $git -C $source status --porcelain --untracked-files=all
if ($LASTEXITCODE -or $dirty) { throw 'Installer source modified.' }
$installer = Join-Path $root 'installer-env'
& (Join-Path $tools 'python.exe') -m venv $installer
if ($LASTEXITCODE) { throw 'Installer environment creation failed.' }
$python = Join-Path $installer 'Scripts/python.exe'
& $python -m pip --isolated --disable-pip-version-check install --only-binary=:all: -r (Join-Path $source 'requirements-managed.txt')
if ($LASTEXITCODE) { throw 'Installer dependency installation failed.' }
Set-Location -LiteralPath $source
& $python -B scripts/install-worker.py
exit $LASTEXITCODE
