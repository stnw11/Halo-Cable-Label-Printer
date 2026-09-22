<#
.SYNOPSIS
    Read-only survey of a Windows print host. Changes nothing, prints nothing.

.DESCRIPTION
    Run this after installing the Brady driver and before installing the
    agent service. It answers most of the driver spike (spec open item 1)
    WITHOUT the printer being physically connected, because the questions
    that block the build are about the DRIVER, not the hardware:

      * Does the Brady driver present as an ordinary Windows printer?
      * What page sizes does it expose, and will it accept a custom size
        matching our rendered document? This is the question that decides
        whether we can hand it a plain PDF at exact media dimensions.
      * Is Brady Workstation installed alongside it, and does it expose an
        automation entry point we could drive instead?
      * Can the account that will run the service actually SEE the printer?

    It also reports everything the agent needs from this machine: Python,
    the queue folder, the share, and the current spooler state.

    Nothing here submits a print job. See -Explain for how to test the
    actual print path against a paused queue.

.PARAMETER PrinterName
    The printer to inspect. Defaults to reading printer_name out of
    config\agent.yaml, then to any printer whose name contains "Brady".

.PARAMETER QueueRoot
    The queue folder to check. Defaults to reading queue_root out of
    config\agent.yaml.

.PARAMETER Json
    Emit machine-readable JSON as well as the human report. Paste this back
    for analysis.

.EXAMPLE
    .\check-windows-host.ps1
    .\check-windows-host.ps1 -Json | Out-File survey.json
#>
[CmdletBinding()]
param(
    [string]$PrinterName,
    [string]$QueueRoot,
    [string]$PythonExe = 'python',
    [switch]$Json,
    [switch]$Explain
)

$ErrorActionPreference = 'Continue'
$AgentDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigYml = Join-Path $AgentDir 'config\agent.yaml'
$report    = [ordered]@{}

function Section { param($t) Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Ok      { param($m) Write-Host "  [ok]   $m" -ForegroundColor Green }
function Warn    { param($m) Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Bad     { param($m) Write-Host "  [FAIL] $m" -ForegroundColor Red }
function Info    { param($m) Write-Host "         $m" -ForegroundColor Gray }

function Read-YamlValue {
    param($Path, $Key)
    if (-not (Test-Path $Path)) { return $null }
    $m = Select-String -Path $Path -Pattern "^\s*$Key\s*:\s*'?`"?([^'`"#]+)" | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim() }
    return $null
}

if ($Explain) {
    Write-Host @'
TESTING THE PRINT PATH WITHOUT THE PRINTER
==========================================

The printer being absent does not stop you proving the whole software
chain. Two techniques, in increasing order of what they prove:

1. PAUSED QUEUE
   Pause the Brady print queue, then let the agent print to it normally.
   The job spools and sits there. Get-PrintJob shows it arrived, and its
   page count tells you the document survived the driver intact -- a
   24-label job must show 24 pages, not 1 and not 24 copies of page 1.

     Suspend-Printer -Name '<printer>'
     # ...run the agent, let it print a job...
     Get-PrintJob -PrinterName '<printer>' | Format-List *

   >>> BEFORE YOU CONNECT THE PRINTER NEXT WEEK, PURGE THE QUEUE. <<<
   Otherwise every test job prints at once the moment it comes online.

     Get-PrintJob -PrinterName '<printer>' | Remove-PrintJob

2. PRINT TO FILE
   Point a copy of the printer at a FILE: port. The driver then renders our
   document through its real pipeline and writes printer-ready output to
   disk. This is the strongest available proof that the driver ACCEPTS our
   document, short of ink on media, and it is what answers the render
   format question.

     Add-PrinterPort -Name 'BRADY_TEST_FILE' -PrinterHostAddress $null
     # or create the port as FILE: via the printer properties UI
     Add-Printer -Name 'Brady TEST (file)' -DriverName '<driver name>' -PortName 'FILE:'

   Print to it, give it an output path, then inspect the file's size and
   first bytes. A plausible spool file means the driver understood the job.

WHAT NEITHER TEST PROVES
  Wrap geometry, applicator sequencing, media sensing, and whether the
  print physically lands correctly on the label. Those need the hardware
  and real media.
'@
    exit 0
}

Write-Host "Windows print host survey -- $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor White

# --- identity ----------------------------------------------------------------
Section 'Identity and OS'
$whoami = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$os = (Get-CimInstance Win32_OperatingSystem)
Info "running as:  $whoami$(if($isAdmin){' (elevated)'}else{''})"
Info "host:        $env:COMPUTERNAME"
Info "os:          $($os.Caption) $($os.Version)"
$report.identity = @{ user = $whoami; elevated = $isAdmin; host = $env:COMPUTERNAME; os = $os.Caption }
Warn 'Printer visibility is PER-USER. Whatever this survey sees, the service account may not. Re-check as that account before trusting it.'

# --- python ------------------------------------------------------------------
Section 'Python'
$py = Get-Command $PythonExe -ErrorAction SilentlyContinue
if (-not $py) {
    Bad "'$PythonExe' not found on PATH"
    $report.python = @{ found = $false }
} else {
    $ver = & $PythonExe -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
    if ([version]($ver -replace '^(\d+\.\d+).*','$1') -ge [version]'3.11') { Ok "Python $ver at $($py.Source)" }
    else { Bad "Python $ver is too old; 3.11+ required" }
    $mods = & $PythonExe -c "
import importlib
for m in ('yaml','jsonschema','watchdog','win32print'):
    try:
        importlib.import_module(m); print(m+'=ok')
    except Exception as e: print(m+'=MISSING')
" 2>$null
    $mods -split "`n" | Where-Object { $_ } | ForEach-Object {
        if ($_ -match 'ok$') { Ok $_ } else { Warn "$_  (pip install -r requirements.txt)" }
    }
    $report.python = @{ found = $true; version = $ver; path = $py.Source; modules = $mods }
}

# --- printers ----------------------------------------------------------------
Section 'Printers visible to this account'
$printers = @(Get-Printer -ErrorAction SilentlyContinue)
if (-not $printers) {
    Bad 'no printers enumerable at all'
} else {
    foreach ($p in $printers) {
        $mark = if ($p.Name -like '*Brady*' -or $p.Name -like '*Wraptor*') { '>>' } else { '  ' }
        Write-Host ("  $mark {0,-40} driver={1} port={2}" -f $p.Name, $p.DriverName, $p.PortName)
    }
}
$report.printers = $printers | ForEach-Object {
    @{ name=$_.Name; driver=$_.DriverName; port=$_.PortName; shared=$_.Shared; type=$_.Type }
}

if (-not $PrinterName) { $PrinterName = Read-YamlValue $ConfigYml 'printer_name' }
if (-not $PrinterName) {
    $guess = $printers | Where-Object { $_.Name -like '*Brady*' -or $_.Name -like '*Wraptor*' } | Select-Object -First 1
    if ($guess) { $PrinterName = $guess.Name; Info "no printer_name configured; inspecting $PrinterName" }
}

# --- the target printer ------------------------------------------------------
Section 'Target printer'
if (-not $PrinterName) {
    Bad 'no target printer identified. Install the Brady driver, or pass -PrinterName.'
    $report.target = @{ resolved = $false }
} else {
    $target = $printers | Where-Object { $_.Name -eq $PrinterName } | Select-Object -First 1
    if (-not $target) {
        Bad "'$PrinterName' is NOT visible to this account"
        Info "visible: $(($printers.Name) -join ', ')"
        $report.target = @{ resolved = $false; requested = $PrinterName }
    } else {
        Ok "'$PrinterName' resolves"
        Info "driver:  $($target.DriverName)"
        Info "port:    $($target.PortName)"
        Info "status:  $($target.PrinterStatus)"
        Info "shared:  $($target.Shared)"

        $cfg = Get-PrintConfiguration -PrinterName $PrinterName -ErrorAction SilentlyContinue
        if ($cfg) {
            Info "paper:   $($cfg.PaperSize)"
            Info "orient:  $($cfg.DuplexingMode) / $($cfg.Collate)"
        }

        # THE question that decides the render format: what page sizes does
        # this driver accept, and will it take a custom one matching our
        # document's exact media dimensions?
        Section 'Print capabilities (this is the render-format answer)'
        $caps = Get-PrintCapabilities -PrinterName $PrinterName -ErrorAction SilentlyContinue
        if (-not $caps) {
            Warn 'Get-PrintCapabilities returned nothing. The driver may not expose PrintCapabilities; check the printer properties UI for its media list.'
        } else {
            $sizes = @($caps.PageMediaSize)
            Info "$($sizes.Count) media size(s) exposed by the driver:"
            $sizes | Select-Object -First 40 | ForEach-Object {
                $w = if ($_.Width)  { '{0:N3}in' -f ($_.Width  / 96) } else { '?' }
                $h = if ($_.Height) { '{0:N3}in' -f ($_.Height / 96) } else { '?' }
                Write-Host ("      {0,-38} {1} x {2}" -f $_.DisplayName, $w, $h)
            }
            if ($sizes.Count -gt 40) { Info "... and $($sizes.Count - 40) more" }

            $custom = $sizes | Where-Object { $_.DisplayName -match 'custom|user' }
            if ($custom) { Ok "a CUSTOM media size is available -- a plain document at exact dimensions is likely to work" }
            else { Warn "no custom media size listed. We may have to match one of the driver's own Brady media names, which changes how renderer.py sizes pages." }

            $report.capabilities = @{
                count = $sizes.Count
                sizes = $sizes | ForEach-Object { @{ name=$_.DisplayName; width_in=if($_.Width){[math]::Round($_.Width/96,3)}else{$null}; height_in=if($_.Height){[math]::Round($_.Height/96,3)}else{$null} } }
                custom_available = [bool]$custom
            }
        }

        $report.target = @{
            resolved=$true; name=$target.Name; driver=$target.DriverName
            port=$target.PortName; status="$($target.PrinterStatus)"; shared=$target.Shared
        }
    }
}

# --- spooler -----------------------------------------------------------------
Section 'Spooler'
$spooler = Get-Service -Name Spooler -ErrorAction SilentlyContinue
if ($spooler -and $spooler.Status -eq 'Running') { Ok "spooler running" } else { Bad "spooler is $($spooler.Status)" }
if ($PrinterName) {
    $jobs = @(Get-PrintJob -PrinterName $PrinterName -ErrorAction SilentlyContinue)
    if ($jobs) {
        Warn "$($jobs.Count) job(s) already queued on '$PrinterName'"
        $jobs | ForEach-Object { Info "  id=$($_.Id) pages=$($_.PagesPrinted)/$($_.TotalPages) size=$($_.Size) status=$($_.JobStatus)" }
        Warn 'PURGE THESE before connecting the printer, or they all print at once.'
    } else { Ok "no jobs queued on '$PrinterName'" }
    $report.jobs = $jobs | ForEach-Object { @{ id=$_.Id; total_pages=$_.TotalPages; size=$_.Size; status="$($_.JobStatus)" } }
}

# --- Brady software ----------------------------------------------------------
Section 'Brady software'
$bradyPaths = @(
    "$env:ProgramFiles\Brady", "${env:ProgramFiles(x86)}\Brady",
    "$env:ProgramFiles\Brady Corp", "${env:ProgramFiles(x86)}\Brady Corp"
) | Where-Object { Test-Path $_ }
if ($bradyPaths) {
    foreach ($path in $bradyPaths) {
        Ok "found $path"
        Get-ChildItem $path -Directory -ErrorAction SilentlyContinue | ForEach-Object { Info "  $($_.Name)" }
        # An automation entry point here would let us drive Brady's own
        # pipeline instead of printing a document ourselves.
        Get-ChildItem $path -Recurse -Filter '*.exe' -Depth 2 -ErrorAction SilentlyContinue |
            Select-Object -First 25 | ForEach-Object { Info "  exe: $($_.FullName.Replace($path,'...'))" }
    }
} else {
    Info 'no Brady program folder found (the driver alone may be all that is installed, which is fine)'
}
$installed = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
                              'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
             Where-Object { $_.DisplayName -match 'Brady|Wraptor' } |
             Select-Object DisplayName, DisplayVersion
if ($installed) { $installed | ForEach-Object { Ok "$($_.DisplayName) $($_.DisplayVersion)" } }
$report.brady = @{ paths=$bradyPaths; installed=$installed | ForEach-Object { @{ name=$_.DisplayName; version=$_.DisplayVersion } } }

# --- queue -------------------------------------------------------------------
Section 'Queue folder'
if (-not $QueueRoot) { $QueueRoot = Read-YamlValue $ConfigYml 'queue_root' }
if (-not $QueueRoot) {
    Warn 'no queue_root configured yet'
} else {
    Info "queue_root: $QueueRoot"
    if (-not (Test-Path $QueueRoot)) {
        Warn 'does not exist yet (install-service.ps1 creates it)'
    } else {
        Ok 'exists'
        foreach ($sub in 'inbox','processing','done','failed','results','.tmp') {
            $p = Join-Path $QueueRoot $sub
            if (Test-Path $p) {
                $n = @(Get-ChildItem $p -File -ErrorAction SilentlyContinue).Count
                Info ("  {0,-12} {1} file(s)" -f $sub, $n)
            } else { Warn "  $sub missing" }
        }
        try {
            $probe = Join-Path $QueueRoot ".survey-$PID"
            'x' | Out-File $probe -ErrorAction Stop; Remove-Item $probe -Force
            Ok 'writable by this account'
        } catch { Bad "NOT writable by this account: $($_.Exception.Message)" }
    }
    $share = Get-SmbShare -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $QueueRoot }
    if ($share) {
        Ok "shared as \\$env:COMPUTERNAME\$($share.Name)"
        Get-SmbShareAccess -Name $share.Name -ErrorAction SilentlyContinue |
            ForEach-Object { Info "  $($_.AccountName): $($_.AccessRight)" }
        $report.share = @{ name=$share.Name; path=$share.Path }
    } else {
        Warn 'not shared yet -- the Docker host cannot reach it until it is'
    }
}
$report.queue = @{ root=$QueueRoot; exists=(Test-Path $QueueRoot -ErrorAction SilentlyContinue) }

# --- agent service -----------------------------------------------------------
Section 'Agent service'
$svc = Get-Service -Name 'HaloCableLabelAgent' -ErrorAction SilentlyContinue
if ($svc) {
    Ok "installed, status: $($svc.Status)"
    $wmi = Get-CimInstance Win32_Service -Filter "Name='HaloCableLabelAgent'" -ErrorAction SilentlyContinue
    if ($wmi) {
        Info "runs as:   $($wmi.StartName)"
        Info "start:     $($wmi.StartMode)"
        Warn "Re-run this survey AS $($wmi.StartName) to confirm it can see the printer."
    }
} else { Info 'not installed yet' }
$report.service = @{ installed = [bool]$svc; status = if($svc){"$($svc.Status)"}else{$null} }

# --- summary -----------------------------------------------------------------
Section 'Summary'
Write-Host @"
  Run with -Explain for how to test the print path against a PAUSED queue,
  which proves the whole chain without the printer connected.

  The two answers most useful to send back:
    1. The media size list above (decides the render format).
    2. Whether a CUSTOM media size is available.
"@
if ($Json) {
    Write-Host "`n--- JSON ---"
    $report | ConvertTo-Json -Depth 6
}
