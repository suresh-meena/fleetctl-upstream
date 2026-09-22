# KIAC cluster: site context and audit trail

Source: the KIAC user manual ("KIAC Cluster Server: Comprehensive User
Manual", PDF `Instructions and guidelines.pdf`, kept out of git) and live
verification on 2026-09-14 for user `sureshmeena`. Labels below:
`documented`, `document-conflict`, `verified-live`, `verified-by-run`,
`inferred`.

These facts are enforced from `~/.config/fleet/protocols.d/kiac.toml` —
`fleetctl` reads partitions, GPU types, and the account/QOS matrix from
there, refreshed against the live scheduler through `fleetctl queue list
<target>` / `fleetctl protocol show <target>` when the protocol allows it.
This file is the audit trail for how those values were established and
where the manual disagrees with itself; it is not the source fleetctl
reads at runtime, and it should not be copied from directly.

## Source-of-truth model

The KIAC manual contradicts itself (node counts, `short`/`long` time
limits, storage quotas, GPU lists), and some of its examples are wrong
(`--partition=general` does not exist; `--mem=16GB` is invalid Slurm
syntax). Never copy manual values into scripts or protocol files. Resolve
claims by origin, newest evidence first:

1. **Fresh live query** — partitions, nodes, GRES, MaxTime, and accounts
   read this session via `fleetctl queue list` / `fleetctl protocol show`,
   or `sinfo`/`scontrol` through `fleetctl exec --admin`.
2. **Dated verified facts** (below, `verified-live`/`verified-by-run` as of
   2026-09-14) — GPU types per partition and the account/QOS matrix
   confirmed by real jobs, now encoded in `protocols.d/kiac.toml`.
   Re-verify after cluster changes; a fresh live query supersedes these
   within a session.
3. **Slurm syntax** — SchedMD semantics; `fleetctl preflight` encodes these
   offline (see [slurm-sources.md](slurm-sources.md)).
4. **Site policy** (login node is for submission only, prefer `/storage`,
   H200 needs an account) — the manual, applied as rules, never copied as
   literal values.

## Verified cluster facts (2026-09-14, user sureshmeena)

- **Partitions and GPU types** (`verified-live`): `long`→A6000,
  `short`→A5000, `medium`→A5000/A6000/ADA6000, `ada`→ADA6000, `a100`→A100
  (node cn6), `h200`→H200 (node cn10).
- **Accounts** (`verified-live`): this user holds `research` and `chiru`,
  not `freerun` — requesting `freerun` returns "Invalid account or
  account/partition combination".
- **Account/partition policy** (`verified-live` + `verified-by-run`):
  `a100` allows `research` (and `freerun`) only. `chiru` passes `sbatch
  --test-only` cleanly and then **stays pending**, with reason "Job's
  account not permitted to use this partition (a100 allows
  research,freerun_not_chiru)".
- **QOS policy** (`verified-by-run`): `h200` requires `--account=chiru
  --qos=h200_qos`. The default `normal` QOS pends with "Job's QOS not
  permitted to use this partition (h200 allows h200_qos not normal)". The
  working combination — `--account=chiru --qos=h200_qos
  --partition=h200` — ran as job 58822, COMPLETED, on cn10.
- **The `sbatch --test-only` blind spot** (`verified-live`): it validates
  syntax and allocation shape only. It does not enforce account-partition
  or QOS policy, so a job can pass the dry run and then pend forever once
  the scheduler applies its association matrix. A green dry run, or a green
  `fleetctl preflight`, is necessary but not sufficient — after
  any newly granted account/partition/QOS combination, confirm it with a
  real 5-minute smoke job before relying on it, e.g. `sbatch
  --account=... --partition=... --gres=gpu:1 --time=00:05:00
  --wrap='hostname; nvidia-smi -L; sleep 5'`.
- **`long`/`short`/`medium`/`ada` account permissions** (`inferred`): only
  `--test-only`-verified for `research` and `chiru`, not confirmed by a
  real run. Treat a first real submission on these partitions as the
  validation.

## Documented conflicts and their consequences

| Area | What the manual says | What to do |
| --- | --- | --- |
| Compute topology | Opens with 10 compute nodes; later says 11, and wavers between one and two master nodes (also "CDS" vs "SERC" Data Centre); node names are `cn1`-`cn10`. | Never hard-code node counts; read live via `fleetctl queue list` / `sinfo`. |
| Partition table | `long`: cn1,cn4/48h; `short`: cn2,cn5/24h; `medium`: cn3/24h; `a100`: cn6/24h; `ada`: cn7-9/48h; `h200`: cn10/24h. | Seeded into `protocols.d/kiac.toml`; limits stay marked unverified until confirmed live. |
| Time limits | A later section states `short` is 12h and `long` is 24h, contradicting the partition table above. | Live MaxTime decides; both candidates are `document-conflict` until queried. |
| Storage | Recommends `/storage`; one note says 500 GB home + 300 GB storage (expandable); the final page says 200 GB per user — three figures, one path. | Recommend `/storage`; never encode a quota (see Storage below). |
| Example partition | The basic example uses `--partition=general`, which is absent from the manual's own partition table. | `fleetctl preflight` rejects it as an ERROR unless a live query finds it. |
| Memory example | `--mem=16GB`; SchedMD documents K/M/G/T suffixes only, no `GB`. | Write `--mem=16G`; preflight flags `16GB` as invalid syntax. |
| H200 | Batch jobs need `--partition=h200` and a group `--account` ("faculty iisc mail id until astrick" — not a usable string as written); the interactive example adds node `cn10` and `h200_qos`. | Account is mandatory; QOS is resolved by the verification above — `h200_qos` is required for batch jobs too. |
| GPU names | Infrastructure lists A5000, A6000, A100, ADA6000, H200, but the GRES section only validates A5000/A6000/A100 as typed values. | Use the per-partition verified types above; confirm GRES type strings live before relying on a new one. |
| Modules | Text cites `python/3.8.5`/`cuda/11.2`; screenshots show a different Conda/CUDA setup. Software lives in `/apps/software`, modulefiles in `/apps/modulefiles`. | Never hard-code module versions; resolve `module load` lines against `module show` output when available. |
| Cancellation | Gives `scancel <job_id>` correctly, then separately says `kill <JOB ID>`. `kill` signals a Unix PID/process group, not a Slurm job. | Always `scancel <jobid>`, or `fleetctl job cancel`. |
| Account lifecycle | Inactive 90 days → blocked; +90 more → account and data deleted. Violations escalate: warning → privileges revoked → suspension. | Operational guidance only; not something fleetctl enforces. |

## Storage

`/storage` is recommended by the manual over home, but its writability was
never confirmed during the 2026-09-14 verification — do not assume a job
can write there without checking on the login node first. Storage quotas
are `document-conflict` (three figures in the table above); never encode
one in a script, protocol file, or generated template.

## Explicit unknowns

- Actual `long`/`short` MaxTime — the 2026-09-14 sweep did not capture
  them; still `document-conflict` until queried live.
- Whether `research`+`h200`, or other account/QOS combinations, work for
  users other than `sureshmeena` — the matrix above is this user's
  associations only. Account permissions are per-user associations; treat
  evidence gathered under one username accordingly, and re-verify after
  cluster changes.
- Storage quotas (three conflicting manual figures; none verified) and
  `/storage` writability.
- Current module names on the cluster.

## Operational guidance from the manual (policy, not enforced by fleetctl)

Credentials are private; avoid idling on allocated resources; run all jobs
through Slurm with realistic requests; users are responsible for backups
and cleanup; sensitive data needs explicit permission; activity may be
monitored; maintenance is announced (plan jobs around it); avoid piling up
login-node sessions (multiple editor windows); issues go to
server.kiac@iisc.ac.in; inactive accounts are blocked/deleted on the
90+90-day schedule above.
