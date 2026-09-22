<#
.SYNOPSIS
    Update an installed print agent in place, with no arguments to type.

.DESCRIPTION
    For a machine that install.ps1 has already set up. It reads the settings
    of the existing install and hands them back to install.ps1:

      queue path      from config\agent.yaml
      share name      the SMB share pointing at that queue
      share account   the account that share grants Change access to
      service account the account the service runs as
      printer name    left as it is in agent.yaml

    So updating is: unzip the new package, run this (or double-click
    update.cmd), done. Nothing about the deployment is re-entered, and
    nothing is guessed.

    For a FIRST install, run install.ps1 -ShareAccount '<account>' instead:
    there is nothing here to read yet.

.PARAMETER InstallRoot
    Where the agent is installed. Default C:\HaloCableLabel.

.PARAMETER PrinterName
    Change the printer queue name while updating, for when the printer is
    renamed or moved to another port. Omit it to keep the current one.

.PARAMETER Backend
    Switch the print backend while updating: 'gdi' prints through the
    printer's own driver, 'null' prints nothing. Omit it to keep the
    current one.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\HaloCableLabel',
    [string]$PrinterName,
    [ValidateSet('null', 'gdi')]
    [string]$Backend
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'HaloCableLabelAgent'

function Write-Step { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Fail { param($m) Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail 'Run this in an administrator PowerShell, or double-click update.cmd, which asks for elevation.'
}

# install.ps1 sits at the package root, but in a repo checkout it is in
# winagent\ beside this script. Look in both rather than assuming a layout.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Installer = @(
    (Join-Path (Split-Path -Parent $ScriptDir) 'install.ps1')   # package: ..\install.ps1
    (Join-Path $ScriptDir 'install.ps1')                        # repo: beside this script
    (Join-Path $ScriptDir 'winagent\install.ps1')               # run from a package root
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Installer) {
    Fail "Cannot find install.ps1 near $ScriptDir. Unzip the whole package and run update.cmd from the folder it creates."
}

$InstallRoot = $InstallRoot.TrimEnd('\')
$ConfigYml = Join-Path $InstallRoot 'agent\winagent\config\agent.yaml'

Write-Step "Reading the current install under $InstallRoot"
if (-not (Test-Path $ConfigYml)) {
    Fail "No agent.yaml at $ConfigYml, so there is nothing to update. For a first install run:`n    powershell -ExecutionPolicy Bypass -File .\winagent\install.ps1 -ShareAccount '<DOMAIN\account>'"
}
$configLines = [IO.File]::ReadAllLines($ConfigYml)
function Read-Yaml-Value {
    param([string]$Key)
    $line = $configLines | Where-Object { $_ -match "^\s*$Key\s*:" } | Select-Object -First 1
    if (-not $line) { return '' }
    ($line -replace "^\s*$Key\s*:\s*", '' -replace '\s+#.*$', '').Trim().Trim("'").Trim('"')
}
$queueRoot = Read-Yaml-Value 'queue_root'
if (-not $queueRoot) { Fail "agent.yaml has no queue_root; run install.ps1 instead." }
Write-Host "    queue:   $queueRoot"

# The share that points at the queue, and who it lets in. Taking these from
# Windows rather than asking again is the point of this script.
$share = Get-SmbShare | Where-Object { $_.Path -and $_.Path.TrimEnd('\') -eq $queueRoot.TrimEnd('\') } | Select-Object -First 1
if (-not $share) { Fail "No SMB share points at $queueRoot. Run install.ps1 -ShareAccount '<account>' to set one up." }
Write-Host "    share:   $($share.Name)"

$adminsName = (New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')).Translate([Security.Principal.NTAccount]).Value
$shareAccount = Get-SmbShareAccess -Name $share.Name |
    Where-Object { $_.AccessRight -ne 'Full' -and $_.AccountName -ne $adminsName -and $_.AccessControlType -eq 'Allow' } |
    Select-Object -ExpandProperty AccountName -First 1
if (-not $shareAccount) { Fail "Share '$($share.Name)' grants no account Change access, so the Docker side could not be using it. Run install.ps1 -ShareAccount '<account>'." }
Write-Host "    account: $shareAccount"

$serviceAccount = 'LocalSystem'
$svc = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
if ($svc -and $svc.StartName) { $serviceAccount = $svc.StartName }
Write-Host "    service runs as: $serviceAccount"

$current = Read-Yaml-Value 'printer_name'
if ($PrinterName) {
    Write-Host "    printer: $PrinterName (was $current)"
} elseif ($current) {
    Write-Host "    printer: $current (kept)"
}

Write-Step 'Running install.ps1 with those settings'
$installArgs = @{
    ShareAccount   = $shareAccount
    ShareName      = $share.Name
    InstallRoot    = $InstallRoot
    ServiceAccount = $serviceAccount
}
if ($PrinterName) { $installArgs.PrinterName = $PrinterName }
if ($Backend) { $installArgs.Backend = $Backend }
& $Installer @installArgs
exit $LASTEXITCODE
