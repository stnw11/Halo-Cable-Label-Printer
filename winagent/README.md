# Windows print agent

Watches the cable label queue and prints each job through the Brady driver
to the Wraptor A6200. Runs on the machine with the driver installed.

It never talks to Halo and never holds Halo credentials. It knows only the
queue protocol in [`protocol/`](../protocol/).

## Why this machine hosts the share

The queue lives on this machine, and the Docker service reaches it over
SMB. That is deliberate:

- **Failure domain.** If this machine is down, nothing prints regardless of
  where the queue lives, so hosting it here adds no new way to fail. A NAS
  would add a third device that can take the system down while both
  endpoints are healthy.
- **Watcher reliability.** Windows change notifications drop events over
  SMB under load and after reconnects. Watching a *local* folder is
  reliable, and it puts the network hop on the write side where retries are
  possible and errors are immediate.

## Install

Build the package on any machine with the repo:

```bash
python tools/package_agent.py        # -> out/HaloCableLabelAgent/ and out/HaloCableLabelAgent.zip
```

Copy the folder (or the zip, then unzip it) to the print host. In an
administrator PowerShell, from that folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -ShareAccount 'DOMAIN\svc-cablelabel'
```

`-ShareAccount` is the account the Docker side connects to the share as.
`install.ps1` does the rest, under `C:\HaloCableLabel`:

- restricts `C:\HaloCableLabel` to Administrators and SYSTEM (Users can
  only read), because the service runs its code as SYSTEM; then copies the
  agent code to `agent\`. A re-run replaces the code and keeps `agent.yaml`;
- creates the queue at `queue\`, and shares it as `\\<host>\CableLabelQueue`
  for the share account and Administrators only;
- sets folder permissions: the share account can modify the queue, but can
  only read `queue\.agent`, which holds the agent's ledger and log;
- enables the File and Printer Sharing firewall rules for Domain and Private
  networks, and warns if the network is set to Public;
- finds Python 3.11+ installed for all users, or installs 3.12 that way with
  winget. A per-user Python is never used, since its owner could change what
  the SYSTEM service runs. If 3.12 is already installed for the current user
  only, Python's installer cannot add it for all users; the script stops and
  says so, and `-ReplaceUserPython` removes the per-user copy first;
- writes `agent.yaml`, setting the queue path and, with `-PrinterName`, the
  printer queue name; downloads NSSM; installs and starts the service;
- writes `C:\HaloCableLabel\queue-share.env`, the share settings for the
  Docker side (everything but the password). Copy it to the Docker project
  folder and run `python tools/import_share_config.py queue-share.env`.

## Updating

On a machine that is already set up, unzip the new package and double-click
`update.cmd` (or run `winagent\update.ps1` in an administrator PowerShell).
It reads the current install's queue path, share name, share account,
service account and printer name, and re-runs `install.ps1` with them, so an
update needs no arguments and cannot drift from the original setup.

The printer queue name is configuration, in `agent.yaml`, because it changes
when a printer is renamed, reconnected or moved to another port. Set it at
install time with `install.ps1 -PrinterName '<name>'`, or change it later
without re-entering anything else:

```powershell
.\winagent\update.ps1 -PrinterName 'Wraptor A6200-PGA000000000000'
```

If the configured name does not exist as a queue and the machine has
exactly one Wraptor, the installer picks that one, so a reconnected or
renamed printer usually needs no argument at all. `Get-Printer |
Select-Object Name` lists the exact names. The agent checks
the name resolves at startup for a real backend, and logs the printers it
can see when it does not.

Install the Brady Wraptor A6200 driver before the agent prints for real. If
the service runs as a named account rather than LocalSystem, pass
`-ServiceAccount 'DOMAIN\svc-account'`, and make sure that account can see
the printer: printer connections are per-user.

With a real backend, the installer checks the printer is visible before it
installs anything. Printer drivers are per-user, and a driver installed under
an interactive admin session is routinely invisible to a service account.
That is the most likely deployment failure here, so it is checked by name
rather than mentioned in passing. The agent re-checks it at startup and
lists the printers it *can* see.

## Backends

| Backend | Status | Notes |
|---|---|---|
| `null` | Working | Validates and logs, prints nothing. **Start here.** |
| `gdi` | Working | **The backend.** Draws the rendered image through the printer's own driver; nothing else is needed on this machine. |
| `brady` | Not implemented | Brady Workstation automation, if the driver proves unsuitable. |

Installing or updating leaves the agent printing through `gdi`. An install
that says `null` is upgraded to `gdi` on the next update, because printing
nothing is a bring-up state rather than a destination. To stay there
deliberately, install or update with `-Backend null`.

`gdi` draws each page at its exact physical size, computed from the media's
dpi, so "do not scale" is arithmetic rather than a checkbox someone can get
wrong in a print dialog. It also sets the document name, which is what the
Wraptor lists in the stored-job menu people pick from.

## Running by hand

```powershell
python src\agent.py --config config\agent.yaml --once    # drain the inbox and exit
python src\agent.py --config config\agent.yaml           # watch continuously
```

## Pacing

The printer stores each job until someone selects it, and takes a while to
ingest a large one. A job handed over while it is still busy is **dropped
silently**: Windows reports it printed, and it never appears in the menu.
Four 500-label jobs sent within a minute left only the first.

So the agent leaves `job_interval_seconds` (60 by default) between finishing
one job and starting the next, waiting rather than dropping work. A run of
several batches therefore takes a few minutes to appear at the printer. Set
it to 0 to disable the wait.

## What it guarantees

- **A job is never printed twice.** Job ids are recorded in a SQLite ledger
  *before* printing, so a crash mid-print looks like a replay on restart and
  is refused.
- **An interrupted job is never retried automatically.** The agent cannot
  know how many labels already came out of the applicator, so anything left
  in `processing/` is moved to `failed/` and a human decides.
- **A corrupt or truncated payload is never printed.** Every job is checked
  against the SHA-256 in its sidecar.

## What `success` does not mean

It means the document was handed to the Windows print subsystem without
error. It does not mean ink reached media. No driver-mediated path can
promise that.
