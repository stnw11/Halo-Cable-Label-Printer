<#
.SYNOPSIS
    Install the Halo cable label print agent on a Windows print host: code,
    queue folder, share, permissions, firewall, Python, config and service.

.DESCRIPTION
    Copy the agent package folder to the print host, then in an
    ADMINISTRATOR PowerShell, from that folder:

        powershell -ExecutionPolicy Bypass -File .\install.ps1 -ShareAccount 'DOMAIN\svc-cablelabel'

    It does, in order:
      1. Checks the share account exists.
      2. Restricts <InstallRoot> to Administrators and SYSTEM (Users read
         only), then copies the agent code to <InstallRoot>\agent. On a re-run
         it replaces the code and keeps the existing config\agent.yaml.
      3. Creates the queue at <InstallRoot>\queue, with its subfolders.
      4. Sets folder permissions: Administrators and SYSTEM full control, the
         share account can modify the queue, and can only READ .agent, which
         holds the agent's ledger and log.
      5. Shares the queue as \\<this host>\<ShareName>, for the share account
         and Administrators only.
      6. Enables the File and Printer Sharing firewall rules for the Domain
         and Private profiles, and warns if the network is set to Public.
      7. Finds Python 3.11+ installed for all users (under Program Files), or
         installs 3.12 that way with winget. A per-user Python is never used:
         the service runs it as SYSTEM, and its owner could change it.
      8. Writes config\agent.yaml from the example if there is none, pointing
         it at the queue, and sets printer_name: from -PrinterName, or from
         the only Wraptor queue on the machine when the configured name does
         not exist. A new install starts on backend 'null'.
      9. Downloads NSSM, then runs install-service.ps1, which installs the
         dependencies, validates the config and installs the service.
     10. Writes <InstallRoot>\queue-share.env with the share settings for
         the Docker side (everything but the password), for
         tools/import_share_config.py to load into .env.

    Safe to re-run: it updates the code, permissions, share and service in
    place.

.PARAMETER ShareAccount
    The account the Docker side connects to the share as, e.g.
    'DOMAIN\svc-cablelabel'. A local account ('HOSTNAME\user') also works.
    The script does not handle its password.

.PARAMETER InstallRoot
    The parent folder for everything. Default C:\HaloCableLabel.

.PARAMETER ShareName
    The name of the queue share. Default CableLabelQueue.

.PARAMETER PrinterName
    The exact Windows printer queue name, as Get-Printer lists it. Written
    into agent.yaml. Only needed by a real backend; with backend 'null'
    nothing is printed and the name is not checked. Omit it to keep whatever
    agent.yaml already has.

.PARAMETER Backend
    'gdi' (the default) draws the rendered label image straight through the
    printer's own driver, needing nothing on this machine but the driver;
    'null' prints nothing and is for proving the pipeline without using
    media. Omit it and an existing agent.yaml keeps its backend, except that
    'null' is upgraded to 'gdi' -- an install that prints nothing is a
    bring-up state, not a destination.

.PARAMETER ReplaceUserPython
    If Python 3.12 is installed for your user only, remove it and install it
    for all users instead. Without this switch the script stops and explains,
    because Python cannot have the same version installed both ways.

.PARAMETER ServiceAccount
    The account the agent service runs as. LocalSystem is fine while the
    backend is 'null'. A real printer needs an account that can see it; you
    will be asked for its password.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ShareAccount,
    [string]$InstallRoot    = 'C:\HaloCableLabel',
    [string]$ShareName      = 'CableLabelQueue',
    [string]$ServiceAccount = 'LocalSystem',
    [string]$PrinterName,
    [ValidateSet('null', 'gdi')]
    [string]$Backend,
    [switch]$ReplaceUserPython
)

$ErrorActionPreference = 'Stop'
$ServiceName = 'HaloCableLabelAgent'
$NssmUrl     = 'https://nssm.cc/release/nssm-2.24.zip'
$SidAdmins   = 'S-1-5-32-544'
$SidSystem   = 'S-1-5-18'
$SidUsers    = 'S-1-5-32-545'

function Write-Step { param($m) Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Note { param($m) Write-Host "    $m" -ForegroundColor Yellow }
function Fail { param($m) Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

# --- 0. Preconditions -----------------------------------------------------------
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail 'Run this in an administrator PowerShell (right-click PowerShell > Run as administrator).'
}

# The package root is the folder holding protocol\ and winagent\. This script
# sits at the package root, or in winagent\ when run from a repo checkout.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PkgRoot = if (Test-Path (Join-Path $ScriptDir 'winagent')) { $ScriptDir } else { Split-Path -Parent $ScriptDir }
foreach ($required in @('protocol\queue.py', 'winagent\src\agent.py', 'winagent\install-service.ps1')) {
    if (-not (Test-Path (Join-Path $PkgRoot $required))) {
        Fail "This does not look like the agent package: $required is missing under $PkgRoot."
    }
}

$InstallRoot = $InstallRoot.TrimEnd('\')
$CodeDir   = Join-Path $InstallRoot 'agent'
$QueueDir  = Join-Path $InstallRoot 'queue'
$AgentDir  = Join-Path $CodeDir 'winagent'
$ConfigYml = Join-Path $AgentDir 'config\agent.yaml'
$NssmExe   = Join-Path $CodeDir 'nssm.exe'

# --- 1. The share account -------------------------------------------------------
Write-Step "Checking the share account $ShareAccount"
try {
    $shareSid = (New-Object Security.Principal.NTAccount($ShareAccount)).Translate([Security.Principal.SecurityIdentifier])
} catch {
    Fail "Windows cannot find the account '$ShareAccount'. Use DOMAIN\user for a domain account (this machine must be able to reach the domain), or $env:COMPUTERNAME\user for a local one."
}
# Normalise to the DOMAIN\user form Windows itself uses.
$ShareAccount = $shareSid.Translate([Security.Principal.NTAccount]).Value
Write-Host "    found: $ShareAccount"
$serviceIsSystem = $ServiceAccount -in @('LocalSystem', 'NT AUTHORITY\SYSTEM', 'SYSTEM')
if (-not $serviceIsSystem) {
    try {
        $svcSid = (New-Object Security.Principal.NTAccount($ServiceAccount)).Translate([Security.Principal.SecurityIdentifier])
    } catch {
        Fail "Windows cannot find the service account '$ServiceAccount'."
    }
}

function Set-Acl-Exact {
    # Replace inherited permissions with exactly these grants. SIDs are used
    # for the built-in groups so this works on non-English Windows too.
    param([string]$Path, [string[]]$Grants)
    $icaclsArgs = @($Path, '/inheritance:r', '/grant:r') + $Grants + @('/Q')
    & icacls @icaclsArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "Setting permissions on $Path failed (icacls exit code $LASTEXITCODE)." }
}

# --- 2. Agent code --------------------------------------------------------------
Write-Step "Installing the agent code to $CodeDir"
# The service runs this code as SYSTEM (or -ServiceAccount), so only
# Administrators and SYSTEM may change it. A new folder under C:\ would
# otherwise inherit Modify for Authenticated Users, and any user could edit
# agent.py, agent.yaml or nssm.exe and have it run as SYSTEM. Set before the
# copy so every file lands with these permissions; queue\ and queue\.agent
# get their own explicit permissions in step 4.
foreach ($dir in @($InstallRoot, $CodeDir)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}
$rootGrants = @("*${SidAdmins}:(OI)(CI)F", "*${SidSystem}:(OI)(CI)F", "*${SidUsers}:(OI)(CI)RX")
if (-not $serviceIsSystem) { $rootGrants += "*$($svcSid.Value):(OI)(CI)RX" }
Set-Acl-Exact -Path $InstallRoot -Grants $rootGrants
# Clear anything an earlier install left on the files, so they inherit
# exactly the permissions above.
& icacls $CodeDir /reset /T /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "Resetting permissions under $CodeDir failed (icacls exit code $LASTEXITCODE)." }
Write-Host "    $InstallRoot`: Administrators, SYSTEM full; Users read-only"
$samePlace = (Resolve-Path $PkgRoot).Path.TrimEnd('\') -eq $CodeDir
if ($samePlace) {
    Write-Host '    already running from the install folder; nothing to copy'
} else {
    # Copies over the old code. Nothing in the destination is deleted, so an
    # existing config\agent.yaml and nssm.exe are kept.
    & robocopy $PkgRoot $CodeDir /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "Copying the agent code failed (robocopy exit code $LASTEXITCODE)." }
    Write-Host '    copied'
}

# --- 3. Queue folder ------------------------------------------------------------
Write-Step "Creating the queue at $QueueDir"
foreach ($sub in @('', 'inbox', 'processing', 'done', 'failed', 'results', '.tmp', '.agent')) {
    $path = if ($sub) { Join-Path $QueueDir $sub } else { $QueueDir }
    if (-not (Test-Path $path)) { New-Item -ItemType Directory -Path $path -Force | Out-Null }
}
Write-Host '    queue folders ready'

# --- 4. Folder permissions --------------------------------------------------------
Write-Step 'Setting folder permissions'
$shareSidText = "*$($shareSid.Value)"
$queueGrants = @("*${SidAdmins}:(OI)(CI)F", "*${SidSystem}:(OI)(CI)F", "${shareSidText}:(OI)(CI)M")
$agentGrants = @("*${SidAdmins}:(OI)(CI)F", "*${SidSystem}:(OI)(CI)F", "${shareSidText}:(OI)(CI)RX")
if (-not $serviceIsSystem) {
    $queueGrants += "*$($svcSid.Value):(OI)(CI)M"
    $agentGrants += "*$($svcSid.Value):(OI)(CI)M"
}
Set-Acl-Exact -Path $QueueDir -Grants $queueGrants
Set-Acl-Exact -Path (Join-Path $QueueDir '.agent') -Grants $agentGrants
Write-Host "    queue: Administrators, SYSTEM full; $ShareAccount modify"
Write-Host "    queue\.agent (ledger and log): $ShareAccount read-only"

# --- 5. SMB share -----------------------------------------------------------------
Write-Step "Sharing the queue as \\$env:COMPUTERNAME\$ShareName"
$adminsName = (New-Object Security.Principal.SecurityIdentifier($SidAdmins)).Translate([Security.Principal.NTAccount]).Value
$existing = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue
if ($existing) {
    if ($existing.Path.TrimEnd('\') -ne $QueueDir) {
        Fail "A share named $ShareName already exists for $($existing.Path). Remove it (Remove-SmbShare -Name $ShareName) or pass -ShareName."
    }
    Grant-SmbShareAccess -Name $ShareName -AccountName $ShareAccount -AccessRight Change -Force | Out-Null
    Grant-SmbShareAccess -Name $ShareName -AccountName $adminsName -AccessRight Full -Force | Out-Null
    # Only these two should have access to this share.
    Get-SmbShareAccess -Name $ShareName |
        Where-Object { $_.AccountName -notin @($ShareAccount, $adminsName) } |
        ForEach-Object { Revoke-SmbShareAccess -Name $ShareName -AccountName $_.AccountName -Force | Out-Null }
    Write-Host '    share existed; access updated'
} else {
    New-SmbShare -Name $ShareName -Path $QueueDir -ChangeAccess $ShareAccount -FullAccess $adminsName `
        -Description 'Halo cable label print queue' | Out-Null
    Write-Host '    share created'
}
$others = Get-SmbShare | Where-Object { $_.Name -ne $ShareName -and -not $_.Special -and $_.Path -and (Test-Path (Join-Path $_.Path 'inbox')) -and (Test-Path (Join-Path $_.Path 'results')) }
foreach ($old in $others) {
    Write-Note "The share '$($old.Name)' ($($old.Path)) also looks like a cable label queue. If it is left over from"
    Write-Note "an earlier setup, remove it with: Remove-SmbShare -Name '$($old.Name)' -Force"
}

# --- 6. Firewall ------------------------------------------------------------------
Write-Step 'Allowing file sharing through the firewall'
# '@FirewallAPI.dll,-28502' is the File and Printer Sharing group, named by
# resource id so it works in any Windows language.
$rules = Get-NetFirewallRule -Group '@FirewallAPI.dll,-28502' -ErrorAction SilentlyContinue |
         Where-Object { $_.Direction.ToString() -eq 'Inbound' -and $_.Profile.ToString() -match 'Domain|Private|Any' }
if ($rules) {
    $rules | Enable-NetFirewallRule
    Write-Host '    File and Printer Sharing enabled for Domain and Private networks'
} else {
    Write-Note 'No File and Printer Sharing firewall rules were found; if the Docker side cannot connect, check the firewall.'
}
$public = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.NetworkCategory -eq 'Public' }
foreach ($p in $public) {
    Write-Note "The network '$($p.Name)' is set to Public, where file sharing stays blocked. Set it to Private or Domain,"
    Write-Note "e.g. Set-NetConnectionProfile -InterfaceIndex $($p.InterfaceIndex) -NetworkCategory Private"
}

# --- 7. Python --------------------------------------------------------------------
function Get-PythonVersion {
    param([string]$Exe)
    try {
        $v = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { return [version]($v | Select-Object -First 1) }
    } catch { }
    return $null
}
function Find-Python {
    # Only a Python installed for all users, under Program Files. The service
    # runs this interpreter as SYSTEM; a per-user Python (in a profile, or the
    # Microsoft Store stub) can be changed by that user WITHOUT elevation, and
    # their change would then run as SYSTEM.
    $candidates = @()
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -like "$env:ProgramFiles\*") { $candidates += $cmd.Source }
    $candidates += @(Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
                     Sort-Object FullName -Descending | ForEach-Object FullName)
    foreach ($exe in $candidates) {
        $v = Get-PythonVersion $exe
        if ($v -and $v -ge [version]'3.11') { return $exe }
    }
    return $null
}

Write-Step 'Finding Python 3.11 or newer, installed for all users'
$python = Find-Python
if (-not $python) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail 'No Python 3.11+ found and winget is not available. Install Python 3.12 from python.org for all users, then re-run.'
    }
    Write-Host '    none found for all users; installing Python 3.12 for all users with winget'
    Write-Host '    (a Python installed for one user only is not used: the service runs as SYSTEM)'

    # Python's installer cannot install a version for all users while the same
    # version is installed for this user: it treats the request as a change to
    # the per-user install, installs nothing, and fails (winget reports 1601).
    $userPython = Get-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
                  Where-Object { $_.DisplayName -match '^Python 3\.12\.\d+ \(64-bit\)$' } | Select-Object -First 1
    if ($userPython) {
        if (-not $ReplaceUserPython) {
            Fail @"
$($userPython.DisplayName) is installed for your user only, and Python's installer
cannot add the same version for all users alongside it. Re-run with -ReplaceUserPython
to have this script remove the per-user copy and install it for all users instead.
Anything else of yours that uses that per-user Python would then use the all-users one.
"@
        }
        $uninstall = if ($userPython.QuietUninstallString) { $userPython.QuietUninstallString } else { "$($userPython.UninstallString)" }
        if (-not $uninstall.Trim()) { Fail "Cannot find the uninstaller for $($userPython.DisplayName); remove it in Settings > Apps and re-run." }
        # The agent service may be running this very Python; stop it first so
        # its files are not in use. install-service.ps1 starts it again later.
        if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
            Write-Host "    stopping the $ServiceName service"
            Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        }
        Write-Host "    removing $($userPython.DisplayName) (per-user), as -ReplaceUserPython asked"
        # Split "C:\...\Package Cache\{id}\python-3.12.x-amd64.exe" /uninstall /quiet
        # into program and arguments; the path contains spaces.
        if ($uninstall -match '^\s*"([^"]+)"\s*(.*)$') { $exe, $exeArgs = $Matches[1], $Matches[2] }
        elseif ($uninstall -match '^\s*(\S+\.exe)\s*(.*)$') { $exe, $exeArgs = $Matches[1], $Matches[2] }
        else { Fail "Cannot read the uninstall command for $($userPython.DisplayName): $uninstall" }
        if ($exeArgs -notmatch '/quiet') { $exeArgs = "$exeArgs /quiet".Trim() }
        $proc = Start-Process -FilePath $exe -ArgumentList $exeArgs -Wait -PassThru
        if ($proc.ExitCode -ne 0) { Fail "Removing $($userPython.DisplayName) failed (exit code $($proc.ExitCode)). Remove it in Settings > Apps and re-run." }
    }

    & winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
    $wingetExit = $LASTEXITCODE
    $python = Find-Python
    if (-not $python) { Fail "Installing Python 3.12 for all users failed (winget exit code $wingetExit). Install it from python.org, choosing 'Install for all users', and re-run." }
}
Write-Host "    using $python ($(Get-PythonVersion $python))"

# --- 8. agent.yaml ----------------------------------------------------------------
Write-Step 'Configuring the agent'
if (-not (Test-Path $ConfigYml)) {
    Copy-Item (Join-Path $AgentDir 'config\agent.example.yaml') $ConfigYml
    Write-Host '    created config\agent.yaml from the example'
}
$yamlQueue = $QueueDir -replace "'", "''"
$found = $false
$lines = foreach ($line in [IO.File]::ReadAllLines($ConfigYml)) {
    if (-not $found -and $line -match '^\s*queue_root\s*:') { $found = $true; "queue_root: '$yamlQueue'" } else { $line }
}
if (-not $found) { $lines = @("queue_root: '$yamlQueue'") + $lines }
[IO.File]::WriteAllLines($ConfigYml, [string[]]$lines)   # UTF-8, no BOM
# Nothing given? If what agent.yaml names does not exist as a queue but
# exactly one Wraptor does, take that one. A print host has one of these,
# and its queue name carries the serial or port, so it changes whenever the
# printer is reconnected or renamed. Only an unambiguous match is used, and
# an existing, resolvable name is never overridden.
if (-not $PrinterName) {
    $configured = ($lines | Where-Object { $_ -match '^\s*printer_name\s*:' } | Select-Object -First 1)
    $configuredName = if ($configured) { ($configured -replace '^\s*printer_name\s*:\s*', '' -replace '\s+#.*$', '').Trim().Trim("'").Trim('"') } else { '' }
    $queues = @(Get-Printer -ErrorAction SilentlyContinue)
    if ($configuredName -and -not ($queues | Where-Object { $_.Name -eq $configuredName })) {
        $wraptors = @($queues | Where-Object { $_.Name -like '*Wraptor*' -or $_.DriverName -like '*Wraptor*' })
        if ($wraptors.Count -eq 1) {
            $PrinterName = $wraptors[0].Name
            Write-Host "    printer_name $configuredName does not exist; using the only Wraptor queue found"
        } elseif ($wraptors.Count -gt 1) {
            Write-Note "printer_name $configuredName does not exist, and there are $($wraptors.Count) Wraptor queues."
            Write-Note "Re-run with -PrinterName '<name>'. Queues: $(($wraptors | ForEach-Object Name) -join ', ')"
        }
    }
}

function Set-Yaml-Value {
    param([string]$Key, [string]$Value)
    $escaped = $Value -replace "'", "''"
    $found = $false
    $script:lines = foreach ($line in $script:lines) {
        if (-not $found -and $line -match "^\s*$Key\s*:") { $found = $true; "${Key}: '$escaped'" } else { $line }
    }
    if (-not $found) { $script:lines = @("${Key}: '$escaped'") + $script:lines }
    [IO.File]::WriteAllLines($ConfigYml, [string[]]$script:lines)
    Write-Host "    ${Key}: $Value"
}

if ($PrinterName) {
    $yamlPrinter = $PrinterName -replace "'", "''"
    $found = $false
    $lines = foreach ($line in $lines) {
        if (-not $found -and $line -match '^\s*printer_name\s*:') { $found = $true; "printer_name: '$yamlPrinter'" } else { $line }
    }
    if (-not $found) { $lines = @("printer_name: '$yamlPrinter'") + $lines }
    [IO.File]::WriteAllLines($ConfigYml, [string[]]$lines)
    Write-Host "    printer_name: $PrinterName"
}

$currentBackend = ($lines | Where-Object { $_ -match '^\s*backend\s*:' } | Select-Object -First 1)
$currentBackend = if ($currentBackend) { ($currentBackend -replace '^\s*backend\s*:\s*', '' -replace '\s+#.*$', '').Trim().Trim("'").Trim('"') } else { '' }
if (-not $Backend -and $currentBackend -eq 'null') {
    # Nothing was asked for and this install prints nothing. That is the
    # bring-up state; leaving it would mean an update silently keeps the
    # printer idle. Pass -Backend null to stay there deliberately.
    $Backend = 'gdi'
    Write-Host '    backend was null (prints nothing); switching to gdi'
}
if ($Backend) { Set-Yaml-Value -Key 'backend' -Value $Backend }

$backendLine = $lines | Where-Object { $_ -match '^\s*backend\s*:' } | Select-Object -First 1
$backend = if ($backendLine) { ($backendLine -replace '^\s*backend\s*:\s*', '' -replace '\s+#.*$', '').Trim().Trim("'").Trim('"') } else { '' }
Write-Host "    queue_root: $QueueDir"
Write-Host "    backend:    $backend"

# --- 9. NSSM and the service ------------------------------------------------------
Write-Step 'Getting NSSM'
if (Test-Path $NssmExe) {
    Write-Host "    already present at $NssmExe"
} else {
    $arch = if ([Environment]::Is64BitOperatingSystem) { 'win64' } else { 'win32' }
    $zip  = Join-Path $env:TEMP 'nssm-2.24.zip'
    $out  = Join-Path $env:TEMP 'nssm-extract'
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $NssmUrl -OutFile $zip -UseBasicParsing
        if (Test-Path $out) { Remove-Item $out -Recurse -Force }
        Expand-Archive -Path $zip -DestinationPath $out -Force
        Copy-Item (Join-Path $out "nssm-2.24\$arch\nssm.exe") $NssmExe
        Write-Host "    downloaded to $NssmExe"
    } catch {
        Write-Note "Download from nssm.cc failed ($($_.Exception.Message)); trying winget."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            & winget install -e --id NSSM.NSSM --silent --accept-package-agreements --accept-source-agreements
            $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
            $cmd = Get-Command nssm -ErrorAction SilentlyContinue
            if ($cmd) { Copy-Item $cmd.Source $NssmExe; Write-Host "    installed with winget; copied to $NssmExe" }
        }
        if (-not (Test-Path $NssmExe)) {
            Fail "Could not get NSSM. Download $NssmUrl, copy $arch\nssm.exe to $NssmExe, and re-run."
        }
    }
}

Write-Step 'Installing the service'
$installArgs = @{
    ServiceName    = $ServiceName
    QueueRoot      = $QueueDir
    PythonExe      = $python
    NssmExe        = $NssmExe
    ServiceAccount = $ServiceAccount
}
if ($backend -eq 'null') {
    # backend null prints nothing, so there is no printer to check for yet.
    $installArgs.SkipPrinterCheck = $true
}
& (Join-Path $AgentDir 'install-service.ps1') @installArgs

# Judge by the service itself rather than an exit code: the last native
# command install-service.ps1 runs is `nssm start`, whose exit code is not a
# reliable signal on its own.
$svc = $null
for ($i = 0; $i -lt 10; $i++) {
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq 'Running') { break }
    Start-Sleep -Seconds 1
}
if (-not $svc -or $svc.Status -ne 'Running') {
    Fail "The service is not running. See the messages above, and $QueueDir\.agent\service-stderr.log."
}

# --- 10. What the Docker side needs ---------------------------------------------
$acctDomain, $acctUser = if ($ShareAccount -match '\\') { $ShareAccount -split '\\', 2 } else { '', $ShareAccount }
# A local account on this machine needs no domain on the Docker side.
if ($acctDomain -eq $env:COMPUTERNAME) { $acctDomain = '' }
try { $hostName = [System.Net.Dns]::GetHostEntry($env:COMPUTERNAME).HostName } catch { $hostName = $env:COMPUTERNAME }

$ShareEnv = Join-Path $InstallRoot 'queue-share.env'
# Built as its own string first: inside @( ), a comma after a `+` chain would
# be taken as part of that expression and glue every line into one.
$stamp = "# Written by install.ps1 on $env:COMPUTERNAME, $(Get-Date -Format 'yyyy-MM-dd HH:mm')."
$shareEnvLines = @(
    $stamp,
    '# Copy this file to the Docker host and run:',
    '#     python tools/import_share_config.py queue-share.env',
    '# It updates these values in .env and asks for the password, which is',
    '# deliberately not in this file.',
    "SMB_HOST=$hostName",
    "SMB_SHARE=$ShareName",
    "SMB_USER=$acctUser",
    "SMB_DOMAIN=$acctDomain"
)
[IO.File]::WriteAllLines($ShareEnv, [string[]]$shareEnvLines)   # UTF-8, no BOM

Write-Host @"

Install complete. The agent service '$ServiceName' is running.

  Code:   $CodeDir
  Queue:  $QueueDir, shared as \\$env:COMPUTERNAME\$ShareName
  Logs:   $QueueDir\.agent\agent.log

For the Docker side, copy this file to the Docker project folder:

    $ShareEnv

and run there:  python tools/import_share_config.py queue-share.env
It fills in the share settings in .env and asks for the password of
$ShareAccount.

To remove the service:
    & '$NssmExe' stop $ServiceName; & '$NssmExe' remove $ServiceName confirm
"@ -ForegroundColor Green
