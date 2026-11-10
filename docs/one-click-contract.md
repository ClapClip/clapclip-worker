# Managed Worker installation and updates

This candidate implements unattended installation, boot startup and official-main
updates. It has **not been deployed**. The central V1 contract was reviewed
locally against Web-ClapClip master
`b412aeee84a4105788b4f00a1bc0816313460b73`: 61 Worker contract checks,
typecheck and 12 database-backed SQL checks passed independently. Shared offline
metadata agrees on the central origin and 14 reviewed model records. The packaged
discovery state `reviewed-unified-worker-v1` records this local acceptance only.
It does not establish deployed APIs, model availability, clean-machine installation,
boot/reboot operation or real task success. Setup still stops before password
input, registration or boot-service installation when central discovery is missing
or unavailable. Local tests do not establish online readiness.

## One script, no configuration arguments

After this candidate and its matching backend are released, run from a terminal:

```powershell
# Windows x64: normal UAC consent, no execution-policy bypass.
.\scripts\install-worker.ps1
```

```sh
# Native M-series macOS 13+, or glibc Linux 2.27+ x64/ARM64 with systemd.
sh scripts/install-worker.sh
```

Each platform script can be downloaded alone: source comes only from official
`https://github.com/ClapClip/clapclip-worker.git`, branch `main`. It installs a
SHA-256 pinned official Micromamba binary, isolated native Python 3.13, Node 22,
GPL FFmpeg/FFprobe, Git and per-release pinned Python dependencies. Linux installs
missing curl/CA transport packages through its normal package manager; OpenCV
libraries are supplied in the isolated tools environment. No system Python
replacement, global PATH change or OS protection-policy change is required.
Intel/Rosetta Macs, musl, Windows ARM/32-bit and free-threaded Python are rejected.
All required download encoders are checked before download is considered ready.

Setup prompts only for the account and a hidden permanent password. A terminal
unable to hide input fails instead of echoing it. Public Cognito configuration,
account authorization, unique node identity and approved models are discovered
automatically. Temporary/unconfirmed accounts require account setup first.

`portable-cpu-v1` uses CPU and, when available on M-series, CoreML. It advertises
only loaded/account-authorized capabilities. Native M-series macOS also requests
enhancement with CoreML/CPU and requires licensed enhancement weights; Windows/Linux
exclude enhancement from this CPU profile and retain the DirectML/CUDA requirement. CPU packages never overwrite a
GPU environment. Manual GPU deployment and the local workbench remain separate.

## One backend and the storage boundary

`mediaworker/managed/bootstrap-service.json` is the sole managed discovery record.
Its reviewed origin is Web-ClapClip, `https://www.clapclip.net`; deployment and
availability remain separate from local contract acceptance. No business-site
fallback exists. Managed state rejects another
origin, including one inherited through `API_BASE_URL` or copied from an old node.

Bootstrap, read-only preflight, approved model metadata/authorization, node login,
heartbeat, claims, reports and upload authorization all use this backend. The
backend dispatches work from the websites; Workers do not call their separate
business APIs. Passwords go only to Cognito's dedicated SDK. The backend receives
its ID token; node bearer tokens never accompany cross-origin requests.
Existing manual configurations and the current production runtime are not migrated
by editing this candidate.

The v1 shape uses public `GET /api/worker/bootstrap`, authenticated
`POST /api/worker/preflight`, `GET /api/worker/models` and asset
`GET /api/worker/models/<assetId>`. Registration remains `/api/worker/login`.
Unexpected versions/origins/schema, denied access, missing routes and unavailable
services stop setup. Preflight does not register or rotate a node, touch its
last-seen state or claim a task.

Only reviewed asset IDs, destination names, positive integer sizes and exact
SHA-256 values are accepted. Backend-authorized model redirects must use HTTPS S3
or the exact reviewed GitHub CDN host `release-assets.githubusercontent.com`;
only port 443 and URLs without userinfo or fragments are accepted.
GitHub paths must match `/github-production-release-asset/<numeric-repo>/<hex-or-hyphen-asset>`.
Provider signatures remain opaque, and storage redirects are never followed. The Worker
does not call GitHub's authenticated API or receive the backend's private-repo key;
every storage request has a fresh anonymous client without account headers,
cookies or pools. Exact range/length checks protect resume; expired signatures
are renewed through the backend, corrupt partials discarded. No signed URL,
private model link or GitHub credential is printed or shipped. Model sets are
content-addressed and separate from running code. The catalogue records provenance,
not new commercial rights. See [THIRD_PARTY.md](../THIRD_PARTY.md).

## Machine ownership, credentials and boot services

One host record and global OS lock span checkout paths, users and node IDs.
An installation transaction lock serializes identity/config/credential creation;
native bootstrap also serializes tools installation. Success, command failure
and handled signals release the bootstrap's own lock. A lock left by abrupt
unrecoverable termination requires inspection, never blind removal. Repeat setup
reuses the owned node/service. Unknown legacy installations require controlled
migration, without generic process-name killing or service replacement.

| OS | Host root | True boot mechanism | Service identity |
| --- | --- | --- | --- |
| Windows | `%ProgramData%/ClapClipWorker` | Boot-triggered `ClapClipWorker` task, no login trigger | LocalService |
| M-series Mac | `/Library/Application Support/ClapClipWorker` | System LaunchDaemon `net.clapclip.worker` | Original non-root user |
| Linux | `/var/lib/clapclip-worker` | System unit `clapclip-worker.service`, multi-user target | Original sudo user, or DynamicUser for direct root install |

Only normal UAC/sudo authorization is used. Tools/launcher are administrator-owned;
runtime data belongs to the restricted identity. Linux DynamicUser has a separate
`/var/lib/clapclip-worker-runtime` StateDirectory. Its exact systemd root-owned
private-directory symlink is accepted, keeping immutable tools and host lock out
of that migration. Boot still requires disk unlock and network availability.

Windows credentials are machine-DPAPI sealed under scoped application ACLs. Linux
uses systemd encrypted credentials; if `systemd-creds` is absent, an explicitly
reported root-only 0600 source uses `LoadCredential`. A present store refusing
encryption fails without silent downgrade. macOS uses a 0600 file in a 0700 private
directory because the login keychain can be locked before login. Passwords never
enter argv, environment or public state/logs. Account binding prevents a silent
account change on an existing node.

## Automatic release replacement

A protected launcher starts one restricted manager and one business runtime.
Windows pythonw/venv shims and actual Python children form one verified family:
exact arguments, executable paths, birth times and parent edges must match.
Unknown orphans, inaccessible identity, recycled PIDs and unrelated descendants
fail closed; no blanket allowance for every child Python is used.

Hourly updates accept only official `origin/main`. Unexpected Git configuration,
origin/refspec/branch, dirty source and divergent history are rejected. Each new
release is archived separately, checked for public contents, installed in its own
pinned venv, checked for imports/provider profile and generated ONNX CPU inference,
then locally validates models/tools and repeats read-only authorization. Preparation
failure leaves the old process running. Failed revisions are suppressed for 24 hours;
repeated startup failure backs off and requires explicit resume.

Drain stops new claims and lets accepted tasks, heartbeats, uploads and terminal
ACKs finish under the existing lease. The entire old OS process family must exit
before replacement registration may rotate the token. The candidate loads and
registers behind a closed claim gate. Valid readiness from its matching session
and process permits commit and polling. Startup failure stops only the verified,
still-gated candidate, waits for exit and restores the previous selection. Unknown
identity or unfinished draining never permits a replacement. The manager itself
drains and restarts through the OS to load new code; its stable launcher restores
the prior selection if the new manager fails before startup acknowledgement.
This includes a short gap; it is not zero downtime or Python hot reload.

Credentials, binding, node-token cache, models, shared three execution slots,
control state and work directories live outside releases. Download keeps one
poller; all capabilities share at most three slots. GIF/media and fencing/lease
contracts remain unchanged. A control watcher requests drain even while update
preparation is downloading files.
The runtime independently verifies its manager's executable and creation time.
Manager death, PID reuse or unreadable ownership state stops new polling while
accepted work finishes. Every manager exit keeps the host lock through child
draining; an inaccessible process identity is not treated as proof of exit.

## Status, stop, resume and removal

Setup writes a protected helper. Windows uses
`C:\ProgramData\ClapClipWorker\worker.cmd status`; macOS/Linux use `worker status`
in the host root (sudo where private data requires it). Commands are `version`,
`status`, `stop`, `resume` and `uninstall`. Removing a boot service needs OS admin
authorization. Stop is persistent across reboot and waits for in-flight work.
Resume reuses the same node. Uninstall removes only verified owned OS registration
and keeps data/models/credentials; it never removes a foreign service or prior
deployment.

Status separately reports local definition, actual enabled OS boot registration,
manager identity, a live matching worker session, startup registration, accepted
ordinary queue poll and whether that receipt is recent. Poll receipts are not
media-job success. Install/start return, model-ready, poller-online and local tests
also do not establish media success. Verification never creates production jobs
or sends an extra claim.
