# Deploying to production

Follow this top to bottom. It assumes nothing is installed yet and ends
with a technician requesting labels from Halo and collecting them at the
printer.

There are two machines:

| | Runs | Needs |
|---|---|---|
| **Print host** | The Windows agent, as a service. Hosts the queue folder and shares it. | Windows 10/11 or Server, the Brady driver, network access to the printer |
| **Docker host** | The service that talks to Halo and renders labels. | Linux with Docker Engine and the Compose plugin, outbound HTTPS to Halo, SMB to the print host |

They talk over one SMB share on the LAN. Nothing accepts inbound
connections from outside, and Halo's cloud never reaches into the network.

Work in this order: **Halo, then the print host, then the Docker host.**
Each step needs something from the one before it.

**Installing:** [what to have ready](#before-you-start-what-to-have-ready)
· [1. Halo](#1-halo) · [2. The print host](#2-the-print-host-windows)
· [3. The Docker host](#3-the-docker-host-linux)
· [4. Prove it end to end](#4-prove-it-end-to-end)

**Running it:** [day-to-day, updating, where to look when something is
wrong](#5-day-to-day) ·
**[Recovering from a print host or share outage](#recovering-from-a-print-host-or-share-outage)**
· [building without internet access](#building-without-internet-access)

> **In an outage, go straight to [Recovering from a print host or share
> outage](#recovering-from-a-print-host-or-share-outage).** Nothing is
> urgent: no identifier is ever reissued, so the worst case is gaps in the
> numbering and some ranges to reprint.

---

## Before you start: what to have ready

- [ ] A **domain service account** for the share, for example
      `DOMAIN\svc-cablelabel`, and its password. The Docker host connects to
      the print host as this account. A person's login works but ties the
      system to their password changing.
- [ ] The **account the agent service runs as** on the print host. Start
      with LocalSystem, the default. If the printer only appears for a
      particular user, use that account instead (see step 2.4).
- [ ] A **Halo API application**: client id, client secret and the token
      URL, with asset **read and write** permission.
- [ ] The **printer**, reachable from the print host and set up in Windows
      with the Brady driver.
- [ ] The **label stock's geometry**: overall size, the size and position of
      the opaque printed zone, and the printer's dpi. Brady's part
      definition has these, or measure a label.

---

## 1. Halo

### 1.1 The asset records

One asset record per **cable type**, not per cable. The record is the
definition and the counter for a family of cables: "CAT6 Blue", "DAC 100G".

Put every one of them in a single **asset group**. That group is all the
service ever looks at, so a record outside it can never trigger a print.
Note the group's numeric id; you will need it in step 3.

### 1.2 The custom fields

Add these to the asset types you use. The names are yours to choose; you
will record the numeric ids in step 3.

| Field | Type | Who edits it | Purpose |
|---|---|---|---|
| Cable ID Prefix | Text or dropdown | View only | The prefix, e.g. `BL` |
| Next Cable ID | Integer | View only | The next unissued number |
| Cables to Label | Integer | **The technician** | The trigger |
| Last Label Run | Text | View only | The service's status line |
| Cable Color | Text or dropdown | Optional | Only used to name the print job |

"View only" means view-only on the **form**. The service still writes them
through the API, which is deliberate: the counter belongs to the service,
and a person nudging it by hand would issue duplicate identifiers.

**Cables to Label counts cables, not labels.** `12` consumes 12 identifiers
and prints 24 labels, because both ends of each cable get one.

Set each record's **Cable ID Prefix** and **Next Cable ID** now. Two records
must never share a prefix; step 3.6 checks that for you.

### 1.3 The API application

Configuration → Integrations → Halo API. Create an application with asset
read and write permission, and keep the client id and secret for step 3.

If it authenticates but sees few or no assets, the cause is its
**permissions**, not its login mode. Compare what it can see against an
integration that already works.

---

## 2. The print host (Windows)

### 2.1 Prepare the machine

1. Install the **Brady Wraptor driver** and confirm Windows can see the
   printer: `Get-Printer | Select-Object Name`. Note the exact queue name;
   it usually carries the serial or port, e.g.
   `Wraptor A6200-PGA000000000000`.
2. Print a test page from Windows, so a failure later is ours and not the
   printer's.
3. The machine must stay on. It hosts the queue, and nothing prints while
   it is asleep or off.

### 2.2 Build the agent package

On any machine with this repository:

```bash
python tools/package_agent.py
```

That writes `out/HaloCableLabelAgent/` and `out/HaloCableLabelAgent.zip`:
the agent, the queue protocol it shares with the Docker side, and the
installer. Copy the zip to the print host by any means (USB, file share,
email) and unzip it, for example onto the Desktop.

### 2.3 Install

In an **administrator** PowerShell, from the unzipped folder:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 `
    -ShareAccount 'DOMAIN\svc-cablelabel' `
    -PrinterName 'Wraptor A6200-PGA000000000000'
```

It installs everything under `C:\HaloCableLabel`:

- `agent\` — the code, config and `nssm.exe`, restricted to Administrators
  and SYSTEM, because the service runs this code as SYSTEM;
- `queue\` — the print queue, shared as `\\<host>\CableLabelQueue` for the
  share account and Administrators only.

It also sets folder permissions (the share account may modify the queue but
only read `queue\.agent`, which holds the agent's ledger and log), opens the
File and Printer Sharing firewall rules for Domain and Private networks,
installs Python 3.11+ for all users if it is missing, downloads NSSM, and
installs and starts the service `HaloCableLabelAgent`.

**It prints nothing yet if you pass `-Backend null`.** Without that flag it
installs ready to print, which is what you want for a real deployment.

At the end it writes `C:\HaloCableLabel\queue-share.env`. Keep that file:
step 3.4 needs it.

### 2.4 Check it

```powershell
Get-Service HaloCableLabelAgent                    # Running
Get-Content C:\HaloCableLabel\queue\.agent\agent.log -Tail 5
```

The log should name the backend, the printer and the queue, then say it is
watching the inbox. If it reports it cannot see the printer, the service
account cannot: printer connections are per-user. Either connect the
printer while logged in as that account, install it machine-wide, or
re-run `install.ps1` with `-ServiceAccount 'DOMAIN\account'`.

### 2.5 Check the network path

From the Docker host, or any other machine:

```bash
nc -z <print host> 445      # SMB reachable
```

If it fails, the firewall rules were enabled only for Domain and Private
networks, and this network is probably classed Public on the print host:

```powershell
Get-NetConnectionProfile
Set-NetConnectionProfile -InterfaceIndex <n> -NetworkCategory Private
```

---

## 3. The Docker host (Linux)

### 3.1 Prepare the machine

Docker Engine with the Compose plugin, and outbound access to:

- your Halo tenant over HTTPS;
- the print host over SMB (port 445);
- Docker Hub and PyPI, **at build time only**. If this machine cannot reach
  them, see "Building without internet access" at the end.

### 3.2 Get the code

```bash
git clone <this repo> /opt/halo-cable-label-printer
cd /opt/halo-cable-label-printer
cp .env.example .env
cp config/printers.example.yaml config/printers.yaml
cp config/layout.example.yaml   config/layout.yaml
cp config/fields.example.yaml   config/fields.yaml
```

None of the four copies is committed: they hold your tenant's values.

Lock down the two that hold secrets or tenant ids. Only the user who runs
`docker compose` needs to read them -- the container is given the
variables, never the file:

```bash
chmod 600 .env config/fields.yaml
```

Note also that membership of the `docker` group is equivalent to holding
the share password: the queue is a CIFS volume, so `docker volume inspect`
prints the credential in cleartext. See [SECURITY.md](SECURITY.md).

### 3.3 Fill in `.env`

| Setting | Value |
|---|---|
| `HALO_BASE_URL`, `HALO_AUTH_URL` | Your tenant |
| `HALO_CLIENT_ID`, `HALO_CLIENT_SECRET` | From step 1.3 |
| `HALO_CABLE_ASSET_GROUP_ID` | The group id from step 1.1 |
| `DISPLAY_TIMEZONE` | Your timezone, e.g. `America/Chicago`. Status lines are read by people |
| `LABELS_PER_CABLE` | `2` unless you label one end only |

Nothing caps how many cables someone may request. A request larger than one
batch (250 cables, 500 labels) is taken a batch per poll until it is done,
with the remainder left on the asset and reported in the status line. The
batch size is fixed in the code: it bounds the size of one rendered image,
which the agent has to open on the print host.

The batches arrive at the printer about a minute apart, because the agent
paces them (`job_interval_seconds` in `agent.yaml`): the printer silently
drops a job handed to it while it is still ingesting a big one.

Leave `SHADOW_MODE=0`. Set it to `1` only for a dry run: it logs what it
would reserve and writes nothing.

### 3.4 Point it at the share

Copy `queue-share.env` from the print host (step 2.3) into this folder, then:

```bash
python3 tools/import_share_config.py queue-share.env
```

(That one runs on the host, not in the container: it edits `.env`, and it
uses nothing outside the Python standard library.)

It fills in `SMB_HOST`, `SMB_SHARE`, `SMB_USER` and `SMB_DOMAIN`, and asks
for the share account's password, which it writes to `.env` quoted so
Docker reads it back exactly.

If it warns that the host name will not resolve, put the print host's **IP
address** in `SMB_HOST`: Docker looks the name up itself when it mounts the
share, and a name only this machine's shell can resolve is not enough.

### 3.5 Record the Halo fields and the media

`config/fields.yaml` — the numeric id of each custom field from step 1.2,
with the name Halo shows. Ids are how fields are found; names appear in
error messages and are checked for you in step 3.6.

`config/printers.yaml` — the label geometry from Brady's part definition or
your own measurement, then set `configured: true`. The service refuses to
start while that is false, so a forgotten measurement fails loudly instead
of producing a run of unreadable labels.

`config/layout.yaml` — the identifier format: separator, zero padding, font
size limits, and how many times the identifier repeats on each label. The
defaults are what this deployment uses.

### 3.6 Check the configuration before starting

These run inside the container, so the host needs no Python packages. The
first run builds the image, which takes a couple of minutes:

```bash
docker compose run --rm halo-cable-label-printer python tools/check_halo.py
docker compose run --rm halo-cable-label-printer python tools/check_prefixes.py
```

`check_halo.py` reports whether the credentials work, how much of the
tenant the application can see, whether Halo honours the group scope, and
whether each configured field id is the field it is named as.
`check_prefixes.py` reports any two cable types sharing a prefix, which
would eventually issue the same identifier twice. Both write nothing.

Fix anything they report before starting the service.

### 3.7 Start it

```bash
docker compose up -d      # builds the image on first run
docker compose logs -f
```

The log should show the media, the poll interval, and then quiet polling.
`docker compose ps` should show the container **healthy**; it reports
unhealthy if a poll has not completed in three intervals.

It restarts by itself unless you stop it, and comes back after a reboot.

**Do not run two copies.** The service takes an exclusive lock and refuses
to start if another holds it, because two pollers would reserve the same
identifiers.

---

## 4. Prove it end to end

1. On a test cable type in Halo, set **Cables to Label** to `1`.
2. Within a poll interval, **Last Label Run** reads
   `QUEUED BL-0100..BL-0100 · 1 cables, 2 labels · <time>`, then `SENT`.
3. At the printer, the job appears in the stored-job list named like
   `Blue CAT6 - BL-0100...BL-0100`. Select it and print.
4. Check the labels: the identifier twice, stacked, inside the opaque zone,
   with the clear tail blank.

`SENT` means the job reached the printer, not that labels exist. The
printer holds jobs until someone selects one, so a job never interrupts a
wrap already running.

---

## 5. Day-to-day

**Updating the agent.** Build a new package, copy it to the print host,
unzip it and double-click `update.cmd`. It reads the current install's
settings and reinstalls with them.

**Updating the service.** One command, from anywhere on the Docker host:

```bash
/opt/halo-cable-label-printer/update.sh
```

It pulls, reports any settings the update added, waits for jobs already in
flight to finish, rebuilds, restarts, and waits until the service reports
healthy. `--no-pull` rebuilds what is already on disk; `--now` skips the
wait for in-flight jobs.

**An update cannot overwrite your configuration.** `.env` and
`config/*.yaml` are gitignored, so they are not in the repository and a pull
has nothing to put in their place; the container reads them from a read-only
bind mount, so a rebuild cannot change them either. Only the committed
`*.example` templates move.

The cost of that safety is silence: a setting added upstream appears in
`.env.example` and nowhere else, and your deployment keeps running on the
code's built-in default without anyone mentioning it. `update.sh` runs the
check that mentions it, and you can run it alone at any time:

```bash
python3 tools/check_config.py
```

It compares key names only -- never values, so it cannot print a credential
-- and it writes nothing. It reports settings the template has that you do
not (with the default you are currently getting), settings you have that the
template dropped and which therefore now do nothing, and any live file that
does not exist yet. You decide what to copy across; a deployment should
never end up running on a value nobody chose.

> **If you update by copying files rather than by `git pull`** -- an
> unpacked release, `scp -r`, `rsync` of the whole directory -- then the
> protection above does not apply, because the copy, not git, decides what
> lands. Exclude the live config explicitly:
>
> ```bash
> rsync -a --delete \
>   --exclude '.env' \
>   --include 'config/*.example.yaml' --exclude 'config/*.yaml' \
>   /path/to/new-release/ /opt/halo-cable-label-printer/
> cd /opt/halo-cable-label-printer && ./update.sh --no-pull
> ```
>
> The `--include` must come first: rsync takes the first rule that matches,
> so without it `config/*.yaml` would exclude the new templates too, and the
> drift check would have nothing to compare against. Excluded files are also
> safe from `--delete`.

**When the printer is renamed or moved**, on the print host:

```powershell
.\winagent\update.ps1 -PrinterName '<new queue name>'
```

**When the share account's password changes**, on the Docker host: edit
`SMB_PASS` in `.env`, then recreate the volume, which caches the old
credentials:

```bash
docker compose down
docker volume rm halo-cable-label-printer_cable-queue
docker compose up -d
```

**A failed run.** The identifiers are already consumed; the service never
reuses them, because two cables labelled the same is worse than a gap. The
status line and the log carry the range, and `tools/reprint_range.py`
reprints it without consuming more numbers:

```bash
docker compose run --rm halo-cable-label-printer \
  python tools/reprint_range.py --prefix BL --from 100 --to 111
```

**Where to look when something is wrong.**

Set up a Halo notification or saved list on **Last Label Run** starting
with `FAILED` or `STALLED`. Those are the two words that mean someone has
to do something, and a rule on them is the difference between noticing in
minutes and noticing when a technician asks.

| Symptom | Look at |
|---|---|
| Nothing happens after setting the trigger | `docker compose logs`, on the Docker host |
| Status says `QUEUED` but nothing prints | `C:\HaloCableLabel\queue\.agent\agent.log` |
| Status says `FAILED` | The same log; the reason is also in the status line |
| Jobs pile up in `inbox\` | The agent service is stopped, or cannot see the printer. After 30 quiet minutes the waiting assets say `STALLED` |
| Jobs in `failed\` | A human decides: they consumed numbers and did not print |
| The service will not start after an update or reboot | The print host: the queue share must be mountable at container start |

**The service needs the share to start.** The queue is a CIFS volume, and
Docker mounts it when the container starts -- so if the print host is off,
asleep, or has reset its network profile to Public, the container cannot
start at all. It is not a crash and `restart: unless-stopped` will not
retry it, because the container never ran. `update.sh` waits two minutes
for the share (`SHARE_WAIT_MINUTES` to change that) and then says plainly
what is wrong; once the print host is back, `./update.sh --no-pull` or
`docker compose up -d` starts it.

Nothing is lost in the meantime. Identifiers are consumed only when a job
is written, so a request made while the service is down simply waits on the
asset until it comes back. If the Docker host may reboot while the print
host is unavailable, re-run `docker compose up -d` after the print host is
up -- it is safe to run repeatedly.

---

## Recovering from a print host or share outage

The print host holds the share, so "the share is down" and "the print host
is down" are the same outage. Nothing here is urgent: no identifier is ever
reissued, so the worst case is gaps in the numbering and some ranges to
reprint.

### What happens while it is down

| | |
|---|---|
| Jobs already queued | Stay in `inbox\`, untouched. They print when the host returns |
| Jobs already on the printer | Unaffected -- they are stored on the printer, not the share |
| Requests made during the outage | The numbers are consumed and the asset is set to `FAILED ... could not write to the print queue`. They need a reprint (step 6) |
| Waiting assets, after 30 minutes | Get a `STALLED` line, so the outage is visible in Halo |
| The service | Keeps running and keeps polling. **But if it is restarted or the Docker host reboots, it will not start again until the share is back** -- the queue is a CIFS volume mounted at container start |

A running container reports `healthy` throughout: the heartbeat proves the
poll loop is alive, not that the share is reachable. Use Halo and the log,
not `docker compose ps`, to judge an outage.

### Bringing it back, in order

**1. Bring the print host back and check the network, not just the power.**
On the print host:

```powershell
Get-NetConnectionProfile          # must NOT say Public -- Public blocks SMB
Get-Service HaloCableAgent        # must be Running
```

If the profile reset to Public:

```powershell
Set-NetConnectionProfile -InterfaceAlias '<adapter>' -NetworkCategory Private
```

**2. Prove the share is reachable from the Docker host.** Do this before
touching the service, so a failure here is not mistaken for a service fault:

```bash
nc -z -w 3 <SMB_HOST> 445 && echo reachable
```

**3. Start the service if it stopped.** Safe to run whether it is up or not:

```bash
cd /opt/halo-cable-label-printer && ./update.sh --no-pull
```

It waits for the share, restarts, and waits for healthy. `docker compose up
-d` does the same without rebuilding.

**4. Let it drain.** Within one poll the queued jobs reach the printer and
their assets move from `QUEUED` or `STALLED` to `SENT`. Watch it happen:

```bash
docker compose logs -f
```

**5. Check for orphans.** A job interrupted mid-print is moved to `failed\`
by the agent rather than reprinted, because it may have partly printed:

```bash
docker compose exec halo-cable-label-printer ls /mnt/cable-queue/failed
```

Each one is a human decision: the numbers are consumed, and whether to
reprint depends on how much of it came out.

**6. Reprint what was burned.** Find the ranges either from Halo -- a saved
list of `Last Label Run` starting with `FAILED` -- or from the log:

```bash
docker compose logs | grep 'RESERVED BUT NOT ENQUEUED'
```

Each line carries its own recovery command. Run one per range:

```bash
docker compose run --rm halo-cable-label-printer \
  python tools/reprint_range.py --prefix BL --from 100 --to 103 --asset-id 4711
```

This never advances `Next Cable ID` and never touches `Cables to Label`, so
it cannot double-issue. With `--asset-id` the asset's status becomes
`RESENT` when it prints, so the Halo record matches what physically exists.
Leave `--asset-id` off when printing test media, which should stay invisible
in Halo.

**7. Confirm nothing is left.** No asset should read `FAILED` or `STALLED`,
and the queue should be empty:

```bash
docker compose exec halo-cable-label-printer \
  sh -c 'ls /mnt/cable-queue/inbox /mnt/cable-queue/processing'
```

Finally, check the printer's **Files** menu: jobs sent before the outage may
be sitting there waiting for someone to select them.

### If the outage will be long

Stop the service rather than letting requests burn numbers into a queue
nobody can write to:

```bash
docker compose stop
```

Requests then simply wait on their assets, with `Cables to Label` still set,
and run normally once you start it again.

---

## Building without internet access

If the Docker host cannot reach Docker Hub and PyPI, build the image on a
machine that can, and carry it over:

```bash
# where the internet is, in a clone of this repo
docker compose build
docker save halo-cable-label-printer-halo-cable-label-printer:latest \
  | gzip > cable-label-image.tar.gz

# on the Docker host
gunzip -c cable-label-image.tar.gz | docker load
docker compose up -d --no-build
```

The repository, `.env` and `config/*.yaml` still need to be present on the
Docker host: the image holds the code, not your configuration. That split is
also why an offline update is safe to repeat -- loading a newer image never
touches the files that hold your tenant's values.
