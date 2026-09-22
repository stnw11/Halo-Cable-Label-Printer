# Security

This service holds two sets of credentials, spans two machines, and prints
on a third. What follows is what it trusts, what it does not, and the
handful of things an operator has to get right. It is written for whoever
deploys or audits this, not only for whoever wrote it.

## What is worth protecting

| | Where it lives |
|---|---|
| Halo API client id and secret | `.env` on the Docker host |
| Queue share account password | `.env` on the Docker host, and in Docker's volume metadata |
| Cable identifiers and asset ids | The print queue, and Halo |

None of it is customer data. The realistic worst cases are an attacker
writing to your Halo asset records, or reaching code execution on the print
host — not data theft.

## Trust boundaries

**The print host agent runs as LocalSystem and renders content that comes
off the share.** This is the sharpest edge in the design. Anyone who holds
the share account's password can drop a crafted image into `inbox\`, and a
SYSTEM-level process will open it with Pillow. A parsing vulnerability in
Pillow is therefore a path to SYSTEM on the print host.

What to do about it:

- **Keep Pillow current on the print host.** It is the single most valuable
  patch in this system. `winagent/requirements.txt` allows a range rather
  than pinning, precisely so an update is a reinstall rather than a code
  change.
- Consider `install.ps1 -ServiceAccount` with a dedicated account that has
  printer access, instead of the LocalSystem default. LocalSystem is the
  default because it reliably reaches printers; it is not the safest choice.
- Treat the share account's password as equivalent to code execution on the
  print host, and do not reuse it anywhere.

**The share is the boundary between the two halves.** The Docker service
writes jobs; the agent writes results. Neither trusts the other's *intent*:
payloads are checksummed against their sidecar, results are schema-validated,
job ids must be UUIDs, and a replayed job is rejected by the agent's ledger.
What the checksum cannot do is distinguish a legitimate writer from an
attacker who holds the same credential — it proves integrity, not authority.

**Halo is reached outbound only.** Nothing in this system accepts an inbound
connection from outside the LAN, and Halo's cloud never reaches in.

## Operator checklist

- **`chmod 600 .env`.** It holds both credentials. Only the user who runs
  `docker compose` needs to read it; the container never sees the file,
  only the variables. `config/fields.yaml` deserves the same treatment —
  it holds your tenant's custom field ids.
- **Membership of the `docker` group is equivalent to holding the share
  password.** The queue is a CIFS volume, so the credential is stored in
  Docker's volume metadata and `docker volume inspect` prints it in
  cleartext. This is inherent to mounting SMB through Compose. It is not
  much of a widening — the docker group is already root-equivalent — but it
  should be a deliberate decision rather than a surprise.
- **Restrict the share to the one account that needs it.** `install.ps1`
  does this: the share account gets Change, Administrators get Full, and
  everything else is revoked. Do not add `Everyone` "temporarily".
- **Consider `seal` in the mount options** if the LAN is not trusted. The
  queue mounts with `vers=3.0`, which authenticates but does not encrypt, so
  job content crosses the network in the clear. Credentials are not exposed
  either way; cable identifiers are.
- **Rotate the Halo client secret** if `.env` is ever exposed, and give the
  API application only asset read and write.

## Supply chain

The Docker image builds `FROM python:3.12-slim` without a digest pin, and
both `requirements.txt` files use version ranges rather than a hash-pinned
lockfile. Builds are therefore reproducible in behaviour but not bit for
bit, and a compromised upstream release would be picked up by the next
build. The trade is deliberate — it keeps security updates one rebuild away
rather than one code change away — but an operator who needs reproducible
builds should pin both.

`install.ps1` downloads NSSM over HTTPS from nssm.cc and does not verify a
hash, falling back to winget. Note that the two paths install different
builds: the release `2.24` and winget's `2.24-101-g897c7ad` respectively.

## What is deliberately not protected

- **Identifier gaps.** If anything fails after numbers are reserved, those
  numbers are never reissued. Two cables labelled the same is worse than a
  gap, so the design spends numbers freely to avoid collisions.
- **Automatic reprints.** A job that failed mid-print is never retried
  automatically, because nothing can know how many labels physically came
  out. It waits in `failed\` for a human.
- **Status writes are not scoped to the asset group.** A result carries the
  asset id its status line is written to. A writer on the share could name
  an asset outside the label-automation group, and the service would write a
  status line to it. The blast radius is text in one custom field, and it
  requires the share credential, which is already the more serious problem.

## Reporting a vulnerability

Open a private security advisory on the repository rather than a public
issue.
