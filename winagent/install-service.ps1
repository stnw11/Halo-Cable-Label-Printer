<#
.SYNOPSIS
    Install the Halo cable label print agent as a Windows service.

.DESCRIPTION
    Wraps agent.py with NSSM by default, because it is simpler and far more
    debuggable than a native pywin32 service. Pass -Native to use pywin32's
    own service host instead, for shops that will not install NSSM.

    The important thing this script does is NOT the installation. It is the
    printer visibility check: printer drivers are per-user, and a driver
    installed under an interactive admin session is routinely invisible to a
    service account. That is the single most likely deployment failure here,
    so it gets a named check that fails loudly rather than a paragraph in a
    README that nobody reads until the first job disappears.

.PARAMETER ServiceAccount
    The account the service runs as. Must be the account that can see the
    printer. Defaults to LocalSystem, which frequently CANNOT -- you almost
    certainly want a real account here.

.NOTES
    Normally run by install.ps1, which also creates the queue, its share and
    permissions, and passes every parameter. Run it directly only to
    reinstall the service alone.
#>
[CmdletBinding()]
param(
    [string]$ServiceName    = 'HaloCableLabelAgent',
    [string]$ServiceAccount = 'LocalSystem',
    [SecureString]$Password,
    [string]$QueueRoot      = 'C:\HaloCableLabel\queue',
    [string]$PythonExe      = 'python',
    [string]$NssmExe        = 'nssm',
    [switch]$Native,
    [switch]$SkipPrinterCheck
)

$ErrorActionPreference = 'Stop'
$AgentDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $AgentDir
$AgentPy   = Join-Path $AgentDir 'src\agent.py'
$ConfigYml = Join-Path $AgentDir 'config\agent.yaml'

function Write-Step { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Fail { param($m) Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

Write-Step 'Checking prerequisites'

if (-not (Get-Command $PythonExe -ErrorAction SilentlyContinue)) {
    Fail "Python not found as '$PythonExe'. Install Python 3.11+ and retry, or pass -PythonExe."
}
$pyVersion = & $PythonExe -c "import sys; print('%d.%d' % sys.version_info[:2])"
Write-Host "    Python $pyVersion"
if ([version]$pyVersion -lt [version]'3.11') {
    Fail "Python 3.11 or newer is required; found $pyVersion."
}

if (-not (Test-Path $AgentPy))   { Fail "agent.py not found at $AgentPy" }
if (-not (Test-Path $ConfigYml)) {
    Fail "agent.yaml not found. Copy config\agent.example.yaml to config\agent.yaml and edit it first."
}

Write-Step 'Installing Python dependencies'
# The agent imports these libraries; it never runs the command-line tools
# they install into Scripts\, so where that folder is does not matter.
& $PythonExe -m pip install --quiet --no-warn-script-location --disable-pip-version-check -r (Join-Path $AgentDir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail 'pip install failed.' }

Write-Step "Preparing the queue at $QueueRoot"
foreach ($sub in @('', 'inbox', 'processing', 'done', 'failed', 'results', '.tmp', '.agent')) {
    $path = if ($sub) { Join-Path $QueueRoot $sub } else { $QueueRoot }
    if (-not (Test-Path $path)) { New-Item -ItemType Directory -Path $path -Force | Out-Null }
}
Write-Host "    queue layout ready"

if (-not $SkipPrinterCheck) {
    Write-Step 'Checking the printer is visible to the service account'

    $printerName = (Select-String -Path $ConfigYml -Pattern "^\s*printer_name\s*:\s*'?`"?([^'`"]+)" |
                    ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() } | Select-Object -First 1)
    if (-not $printerName) { Fail "Could not read printer_name from $ConfigYml" }
    Write-Host "    configured printer: $printerName"

    $visible = Get-Printer -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name
    if ($visible -notcontains $printerName) {
        Write-Host "    Printers visible to YOU right now:" -ForegroundColor Yellow
        $visible | ForEach-Object { Write-Host "      $_" }
        Fail @"
The configured printer is not visible even in this session.
Install or connect the Brady Wraptor A6200 driver, then retry.
"@
    }
    Write-Host "    visible in this session: yes"

    if ($ServiceAccount -ne 'LocalSystem') {
        Write-Host @"

    NOTE: the printer is visible to YOU, but this service will run as
    $ServiceAccount. Printer connections are per-user. If the agent reports
    that it cannot see the printer, log in as that account once and connect
    the printer, or install it as a machine-wide (not per-user) printer.
    The agent re-checks this at startup and names the printers it can see.
"@ -ForegroundColor Yellow
    }
}

Write-Step 'Running the agent once to validate configuration'
& $PythonExe $AgentPy --config $ConfigYml --once
if ($LASTEXITCODE -ne 0) { Fail 'The agent failed its startup checks. Fix the errors above before installing the service.' }

if ($Native) {
    Write-Step 'Native service installation'
    Fail @"
-Native is not implemented. Use NSSM (the default), or register agent.py
with your preferred service host and point it at:
    $PythonExe $AgentPy --config $ConfigYml
"@
}

Write-Step "Installing the service '$ServiceName' via NSSM"
if (-not (Get-Command $NssmExe -ErrorAction SilentlyContinue)) {
    Fail "NSSM not found as '$NssmExe'. Download it from https://nssm.cc/, put it on PATH, and retry. This project does not redistribute it."
}

if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
    Write-Host "    existing service found; stopping and removing it"
    # Only stop it if it is running; stopping a stopped service makes NSSM
    # print an alarming but harmless message.
    if ((Get-Service -Name $ServiceName).Status -ne 'Stopped') { & $NssmExe stop $ServiceName confirm | Out-Null }
    & $NssmExe remove $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 2
}

$pythonPath = (Get-Command $PythonExe).Source
& $NssmExe install $ServiceName $pythonPath $AgentPy --config $ConfigYml
& $NssmExe set $ServiceName AppDirectory  $RepoRoot
& $NssmExe set $ServiceName DisplayName   'Halo Cable Label Print Agent'
& $NssmExe set $ServiceName Description   'Watches the cable label queue and prints jobs to the Brady Wraptor A6200.'
& $NssmExe set $ServiceName Start          SERVICE_AUTO_START
& $NssmExe set $ServiceName AppStdout     (Join-Path $QueueRoot '.agent\service-stdout.log')
& $NssmExe set $ServiceName AppStderr     (Join-Path $QueueRoot '.agent\service-stderr.log')
& $NssmExe set $ServiceName AppRotateFiles 1

if ($ServiceAccount -ne 'LocalSystem') {
    if (-not $Password) { $Password = Read-Host -AsSecureString "Password for $ServiceAccount" }
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Password))
    & $NssmExe set $ServiceName ObjectName $ServiceAccount $plain
    $plain = $null
}

& $NssmExe start $ServiceName
Start-Sleep -Seconds 3

$svc = Get-Service -Name $ServiceName
Write-Host "`nService '$ServiceName' is $($svc.Status)" -ForegroundColor Green
