# fleetmon: fleet telemetry

Fleetmon is read-only. One hub on the bridge (numpi) stores bounded snapshots
in SQLite and serves a dashboard and a JSON API on port **8088**. Never change
that port; fleetqd has its own (8089). Direct workstations run only a
stateless `fleetmon snapshot` helper that the hub calls through `fleetctl`;
Slurm login targets are polled with `squeue`/`sacct` through
`fleetctl exec --admin`, unless fleetqd owns that site's observer.

## Asking it questions

From an agent, prefer `fq usage [--idle]` (see [fq.md](fq.md)): it serves
Fleetmon's data from fleetqd's cache with the same verdicts. Use the hub
directly when you need what `fq usage` leaves out, such as who owns a busy GPU.
The hub's URL is not in this repo; ask the user for it, and do not scan for it.

| Endpoint | Gives |
| --- | --- |
| `/api/idle-gpus` | every GPU with `availability` (idle/busy/unknown), `reason`, `owners`, and any `fleetq_job`; a summary; and `unreadable`, hosts whose GPUs NVML could not read |
| `/api/overview` | one row per host: state, CPU, RAM, per-host idle/busy/unknown GPU counts |
| `/api/hosts/<target>` | one host in detail, including its current processes |
| `/api/jobs` | scheduler jobs the hub has observed |
| `/api/hub-status` | poller and backup health |
| `/api/feed/v1/usage` | compact latest metrics with per-GPU verdict (what `fq usage` reads) |
| `/api/feed/v1/capacity` | raw per-GPU samples for fleetq's idle gate |

Every read is served from stored samples and starts no remote call, so
repeating one costs the fleet nothing. Pages are paginated (`limit` 1-500,
`offset`).

## What the states mean

- A GPU is **idle** only after two fresh, consecutive observations with zero
  utilization and no compute process. Anything not proven (stale receipt,
  NVML error, unreachable host, one observation so far) is **unknown**, never
  free.
- A host is `live`, `partial`, `stale`, `unreachable`, `helper_missing`,
  `unsupported_python`, and so on. Only `live` and `partial` data is current.
  `partial` ("live · limited") means the snapshot is missing data, such as
  NVML failing or a process scan cut short; the helper's routine top-80
  process list does not make a host partial.
- A host whose GPUs are `unreadable` (shown in `fq usage` and on the
  dashboard) has a driver problem, most often `nvmlerror_librmversionmismatch`:
  the NVIDIA driver was updated and the machine was not rebooted. Its GPUs are
  missing from every count. Tell the user; rebooting is their decision.
- Fleetmon has no view inside Slurm compute nodes.

## Operating the hub

The hub runs on numpi as the user unit `fleetmon-hub.service`; reach it with
`fleetctl exec --admin <bridge> -- ...`. Local commands there:
`fleetmon status`, `fleetmon doctor`, `fleetmon maintain`, `fleetmon hub --once`.
Installers: `scripts/install-hub` (atomic release switch, never starts the
service) and `scripts/install-helper --target NAME --i-authorize-target-NAME`
(one target at a time, `--dry-run` first). Details are in `fleetmon/README.md`
in the dotfiles repo.
