# Halo-Cable-Label-Printer

Lets a technician request a run of cable labels from **inside HaloITSM**, on
the asset record for the cable type they are labelling, and have them print
and wrap on a Brady Wraptor A6200. Each cable gets a unique identifier of
the form `BL-0100`, and **both ends of every cable get an identical label.**

The service owns the numbering. A technician never picks a number. That is
the point: cable identifiers must be unique, and the only way to guarantee
that is to make one process the sole allocator.

> **Status: pre-release.** The pipeline works end to end, and labels print
> and apply correctly on a Wraptor A6200. What is left is running it in
> anger: a full request from inside Halo on a real cable type, and the
> operational shake-out that follows. See "What is verified" below.

## How it works

A technician sets **Cables to Label** to `12` on a cable type's asset and
saves. That is the entire interaction. Within one poll interval the service
reserves identifiers `BL-0100` through `BL-0111`, renders an image of 24
labels at the printer's dpi, and writes it to a queue folder. A Windows
agent picks it up and draws it through the printer's own driver -- no PDF
reader or other software on the print host.

```
HaloITSM (cloud)          Docker host (Linux)        Windows print host        Wraptor A6200
      |                          |                          |                        |
      |<--- poll over HTTPS -----|                          |                        |
      |---- prefix, counter ---->|                          |                        |
      |<--- reserve (1 write) ---|                          |                        |
      |                          |--- SMB write --> [queue] |                        |
      |                          |                  [queue] --- local watch ---> print via driver
      |                          |<-- result ------ [queue] |                        |
      |<--- status line ---------|                          |                        |
```

Unlike its two sibling projects, this one cannot send raw bytes to the
printer: the Wraptor has no raw-socket mode and is driven by a Windows
driver. Hence the second component and the queue between them.

The label font travels with the project (`assets/fonts`), so a label renders
identically on a laptop, in the container and in a test.

**Every connection is outbound and LAN-local.** The service makes HTTPS
calls to Halo and an SMB connection to a share on the LAN. Nothing listens
for inbound traffic, and Halo's cloud never reaches into the network.

## Two guarantees worth understanding before you deploy

**1. Gaps, never collisions.** Identifiers are reserved in Halo *before* the
labels are rendered. If anything fails after that, the numbers are consumed
and the sequence has a gap. That is deliberate. Reserving after a successful
print would reissue the same identifiers if the service died mid-batch, and
two cables in a rack labelled `BL-0104` is a fault that survives for years.
Nobody audits cable numbers for contiguity; everybody suffers a duplicate.

**2. Exactly one instance.** The reserve is safe only because there is one
writer. Two instances polling the same tenant can both read the counter
before either writes it, and both issue the same block. The service takes an
exclusive lock and refuses to start if another instance holds it. **Do not
run replicas.**

## Setup

Deploying for real? **[DEPLOYMENT.md](DEPLOYMENT.md)** is the ordered,
start-to-finish version of this section, including the Windows print host,
accounts, and what to check at each step. The rest of this file explains
how the thing works and why.


### 1. Halo

Create one asset record per cable type, for example "CAT6 Blue". The asset
is not a cable; it is the definition and counter for a family of cables.
The records can use whatever asset types suit you (CAT6, DAC, display
cable...). Put them all in **one asset group**. The group is what the service
polls, so only records someone deliberately put there can ever trigger a
print.

Give those asset types four custom fields:

| Field | Type | Editable | Purpose |
|---|---|---|---|
| Cable ID Prefix | Text or dropdown | View only | The prefix, e.g. `BL` |
| Next Cable ID | Integer | View only | The next unissued number |
| Cables to Label | Integer | **Agent-editable** | The trigger |
| Last Label Run | Text | View only | The service's status line |
| Cable Color | Text or dropdown | Optional | Only names the print job, e.g. "Blue CAT6 - ..." |

The field names are yours to choose; `config/fields.yaml` records each
field's numeric id and name. A dropdown prefix works: the option's text is
used, not its index.

The service writes Next Cable ID, Cables to Label (back to zero) and Last
Label Run through the API; it only ever reads the prefix. Halo enforces
read-only at the form layer, not the API layer, so this works and keeps
humans from nudging the counter by hand.

**Cables to Label counts cables, not labels.** A value of `12` consumes 12
identifiers and prints 24 labels.

### Large requests

**Ask for any number. There is nothing to configure and nothing to split by
hand.** A request larger than one batch is taken **250 cables (500 labels)
at a time**, one batch per poll, and the remainder stays on the asset until
it is done.

Asking for 1000 cables:

| Poll | Reserves | Next Cable ID | Cables to Label | Last Label Run |
|---|---|---|---|---|
| 1 | `BL-0001`..`BL-0250` | 251 | 750 | `QUEUED ... 250 of 1000 cables, 500 labels ... 750 still to print` |
| 2 | `BL-0251`..`BL-0500` | 501 | 500 | `... 500 still to print` |
| 3 | `BL-0501`..`BL-0750` | 751 | 250 | `... 250 still to print` |
| 4 | `BL-0751`..`BL-1000` | 1001 | 0 | `SENT BL-0751..BL-1000 · 250 cables, 500 labels · ...` |

Four jobs appear at the printer, each picked and printed separately. They
arrive about a minute apart: the printer stores jobs until someone selects
one, and drops a job handed to it while it is still ingesting the last, so
the agent paces them (`job_interval_seconds` in `agent.yaml`).

The batch size is **fixed at 250 and deliberately not configurable**. It is
a technical ceiling rather than a preference: one job is one image with a
page per label, and at 250 cables that image is about 56 million pixels.
Past roughly 400 it crosses the image library's safety limit and the agent
on the print host refuses to open it -- after the numbers have been
consumed. A setting would let someone choose a value that renders fine on
the server and fails at the printer, so the ceiling is the code's to keep,
not the operator's.

Then create an API application (Configuration > Integrations > HaloITSM
API) with asset read and write permission. If it authenticates but sees few
or no assets, the cause is its **permissions**, not its login mode: a working
integration and a broken one were measured both using "Application
identity", and the difference was what each could see. `tools/check_halo.py`
reports what the application can see, with and without the group.

### 2. The Windows print host

Build the agent package with `python tools/package_agent.py`, copy
`out/HaloCableLabelAgent` to the print host, and run its `install.ps1` as
administrator with `-ShareAccount` set to the account the Docker side will
use. It sets up the agent, the queue folder and its share, permissions, the
firewall and the service, and writes `queue-share.env` with the share
settings for the Docker side. Details
are in [`winagent/README.md`](winagent/README.md). Keep `backend: null` until
the whole pipeline works.

### 3. The Docker service

```bash
git clone <this repo> && cd Halo-Cable-Label-Printer
cp .env.example .env
cp config/printers.example.yaml config/printers.yaml
cp config/layout.example.yaml   config/layout.yaml
cp config/fields.example.yaml   config/fields.yaml
```

Edit `.env` with your tenant and the asset group id. Edit
`config/fields.yaml` with the four custom fields' ids and names. Edit
`config/printers.yaml` with your **measured** media geometry and set
`configured: true`. The service refuses to start while that is false —
media geometry cannot be guessed, and a forgotten measurement should fail
at startup rather than produce a run of unreadable labels.

Copy `queue-share.env` from the print host (step 2) into the project folder
and load it:

```bash
python tools/import_share_config.py queue-share.env
```

That fills in the `SMB_*` settings in `.env` and asks for the share
account's password. Docker mounts the share for the container when it
starts; nothing is mounted or stored on the Docker host. Then start it:

```bash
docker compose up -d
docker compose logs -f
```

### 4. Prove it before loading media

```bash
python tools/check_halo.py                    # auth, group scope, field ids and names
python tools/check_prefixes.py                # no two cable types share a prefix
python tools/render_test_block.py             # render locally, then MEASURE it
SHADOW_MODE=1 docker compose up               # see what it would reserve
python tools/enqueue_test_job.py              # a job the agent can print, no Halo
```

With the agent on `backend: null`, set Cables to Label on a test asset and
watch the field go `QUEUED` then `SENT`. Only then load media.

## Feedback

**Last Label Run** is the only feedback the person who pressed the button
sees. The print happens on another machine, behind a driver, seconds to
minutes later, so a log on the Docker host reaches nobody.

```
QUEUED  BL-0100..BL-0111 · 12 cables, 24 labels · 2026-09-18 14:12
SENT    BL-0100..BL-0111 · 12 cables, 24 labels · 2026-09-18 14:12
FAILED  BL-0100..BL-0111 · 12 cables, 24 labels · 2026-09-18 14:12 · printer offline
RESENT  BL-0100..BL-0111 · 12 cables, 24 labels · 2026-09-18 14:12
STALLED BL-0100..BL-0111 · 12 cables, 24 labels · 2026-09-18 14:42 · no response from the print host since 14:12
```

**`STALLED` is the one nobody would otherwise see.** Every other outcome
writes something; a print host that goes away writes nothing, so the job
waits and the asset still reads `QUEUED`, which looks exactly like
"printing shortly". If work is waiting and *nothing* has completed for
`STALLED_AFTER_MINUTES` (30 by default), every waiting asset is told. A
long run is not a stall: each batch that completes resets the clock.

The status word comes first so a saved Halo list filtered to `FAILED` gives
you alerting for free. The range is always present, including on failure,
because it is what the reprint tool needs.

**`SENT` means the document reached the printer, not that labels exist.**
The Wraptor holds each job in its stored-file list until someone selects it
at the printer, which is deliberate: an arriving job never interrupts a wrap
already running. The job appears there under a name built from the asset,
e.g. `Blue CAT6 - BL-0100...BL-0102`, so it can be matched to the
request. No driver-mediated path can promise ink reached media anyway.

## Recovering a failed run

A failed job has already consumed identifiers, and the agent cannot know how
many labels came out before it failed. So nothing is ever re-printed
automatically. Read the range off the `FAILED` status line and:

```bash
python tools/reprint_range.py --prefix BL --from 100 --to 111 --asset-id 4711
```

This never advances the counter and never touches the trigger field.
`--asset-id` is optional and only controls whether the asset's status line
is updated; leave it off for test media.

For a whole outage rather than one job -- the print host down, the share
unreachable, several ranges to recover -- follow the ordered runbook in
**[DEPLOYMENT.md: Recovering from a print host or share
outage](DEPLOYMENT.md#recovering-from-a-print-host-or-share-outage)**.

## Security

Trust boundaries, the operator checklist (`chmod 600 .env`, share ACLs,
keeping Pillow current on the print host) and what is deliberately not
protected are in **[SECURITY.md](SECURITY.md)**.

## Configuration

| File | Holds | Committed? |
|---|---|---|
| `.env` | Secrets, the asset group id, operational settings | No |
| `config/printers.yaml` | Measured media geometry | No |
| `config/layout.yaml` | Identifier format, fonts, legend repetition | No |
| `config/fields.yaml` | The four Halo custom fields: id and name | No |
| `winagent/config/agent.yaml` | Printer name, backend, queue path | No |

Every one ships as a `.example` template. Your naming convention, media
size, prefix pattern, and number padding are all config. If you have to edit
`src/` to use a three-digit sequence or a different separator, that is a bug
worth reporting.

## Catch-up after downtime

Requests queued while the service was down were made by people on purpose,
so printing them on return is correct and intentional.

## What is verified

| | |
|---|---|
| Allocation, queue protocol, agent logic | Covered by 243 automated tests |
| Full cycle, Halo field to status line | Verified against a live Halo tenant |
| Page size and layout | Verified by reading the rendered output back, and on media |
| Docker image | Builds and runs; verified against a live tenant |
| Queue over SMB to the print host | Verified; jobs are picked up in about a second |
| **Physical print on a Wraptor A6200** | **Verified: labels print and apply correctly** |
| Which driver path to use | Settled: the agent draws the rendered image through the printer's own driver (`gdi`) |
| **Media geometry** | **Yours to set. These values match one Brady part; measure or check your own.** |

The label layout was validated on media: the identifier is printed twice,
stacked, in the opaque zone, and reads correctly once wrapped. Both sibling
projects found their real layout bugs on physical media rather than in
tests, so that check mattered more than any test here.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

`tests/test_allocator.py` is the heart of the suite. If you change anything
about how numbers are issued, that file is the review.

`tests/test_no_leaked_values.py` enforces the rule that no site-specific
value reaches this repo. It reads your own local, gitignored configuration,
compares each value against the committed `.example` template, and fails if
anything that differs appears in a file git would commit. It contains no
real values itself, so it works for your deployment as well as ours, and it
skips on a fresh clone with no local config yet.

That is what keeps field ids, tenant URLs, share paths, printer names and
prefixes out of the published tree. All of them are configuration, never
constants. If you find a site-specific value baked into `src/`, that is a
bug worth reporting.

## License

MIT.
