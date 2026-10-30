# fq: the fleet job queue

`fleetqd` is the one scheduler for the fleet; it runs on the bridge (numpi) and
calls `fleetctl` as its only transport. `fq` is its client. Workstation jobs run
under a per-attempt `fq-node` runner; Slurm sites get a job-scoped batch wrapper.
`fq --help` and `fq <command> --help` are complete and authoritative; this page
is the working summary.

## Is it usable from here?

```bash
fq --json whoami        # exit 0: owner, kind, scopes -- use fq for everything below
```

| Result | Meaning | Do |
| --- | --- | --- |
| exit 0 | client configured, daemon up | use `fq` |
| exit 2, `no fleetqd URL` | this machine has no client config | run `fq` on the bridge: `fleetctl exec --admin <bridge> -- fq ...` (reads, cancel, and `--in-place` submits only) |
| exit 5 | daemon unreachable (down, or no route) | do not fall back to ad hoc job control; report it, or use the bridge as above |
| exit 6 | token missing, unreadable or revoked | tell the user; tokens are created on numpi with `fleetqd token create` |

When `fq` runs on the bridge, `--dir .` snapshots the *bridge's* directory.
Either `fleetctl sync push` the code to the target first and submit with
`--in-place <path> --on <target>`, or run `fq` locally.

## Reading the fleet (cached; contacts no machine)

```bash
fq usage --idle            # GPUs Fleetmon proves idle right now
fq usage [machine]         # every GPU: util, VRAM used/total, FREE verdict, sample age
fq nodes                   # fleetq's machines: state, drain, GPUs it has reserved
fq q [-t PD,R] [-w <machine>] [-n 'name*'] [--watch]   # running and waiting jobs, in dispatch order
fq history [--since 24h] [--summary]                   # finished jobs, wait, run time, GPU-hours
fq explain <id>            # why it waits, or where it was placed
```

- `FREE` / `--idle` is Fleetmon's rule: two fresh, consecutive samples with
  zero utilization and no compute process. `?` means not proven either way
  (stale, unreachable, unsupported). Never report a `?` GPU as free.
- An idle GPU may still be reserved by a fleetq allocation; `fq nodes` shows
  reservations. Measured use and reservations are different questions.
- `fq usage` covers machines Fleetmon monitors directly. It has no view into
  Slurm compute nodes; for a cluster, see "Slurm sites" in the skill.
- These reads hit numpi's caches, so repeating them is free. They replace
  `nvidia-smi` over ssh, and `squeue`/`sacct`, for fleetq jobs.

## Submitting

```bash
fq --json submit --idempotency-key "<task>-<config-hash>" --gpus 1 --on <target> \
   --wait --timeout 9m -- python train.py --seed 0
```

- The job's command goes after `--`. `--json` may go anywhere before it.
- `--gpus` is required (`0` for CPU-only). `--on a,b` runs once on any one of
  them; `--each a,b` runs one job per destination; `--array 0-9%4` runs ten
  tasks, at most four at once, each reading `$FQ_ARRAY_TASK_ID`.
- To use every idle GPU, submit the work as one `--array` (or several jobs)
  with `--on` naming the candidate machines, and let fleetqd place it. Do not
  pick GPUs by hand from `fq usage` and pin each run.
- Always pass an idempotency key derived from the task, never a random one. On
  exit 5 retry with the *same* key: it replays the original job instead of
  creating a second.
- `--dry-run` prints the spec (and the upload) and submits nothing.
- `--wait` prints `submitted job N` to stderr before it blocks, so an
  interrupted wait still tells you the id.
- Clusters are used only when the token allows them and the job names one
  (`--on <site>`, `--queue <site>:<queue>`) or passes `--allow-clusters`.
- `--spill-after 2h --spill-to kiac:a100` tries owned machines first and a
  cluster only if the job has not started by then.

## Waiting, logs, outputs

```bash
fq --json wait <id> --timeout 9m     # exit 4 means still running: call it again
fq logs <id> [--err] [--tail BYTES] [-f]
fq --json fetch <id> -o <dir>        # outputs requested with --collect
```

- Block on `fq wait`; never loop `fq q`, `squeue` or `sleep; check`.
- Read the JSON only: `job.terminal`, `job.success`, `job.execution.outcome`,
  `job.execution.exit.code`, `job.artifacts.state`. Compute success and output
  collection are reported separately.
- `fq logs` cleans progress-bar redraws when its output is captured; `--raw`
  gives the exact bytes. `-f` streams line by line through a pipe.
- A cluster job's log arrives on the site's approved cadence; reading more
  often does not make it arrive sooner.

## Changing and stopping jobs

- `fq cancel|hold|release <id>...`, or by filter: `--name 'sweep*'`,
  `--group grp_...`, `--phase PENDING`.
- `fq modify <id> --gpus/--mem/--time/--on/--priority/--begin ...` changes a
  job that has not started; what it runs cannot change.
- `fq top <id>` moves it ahead of your other waiting jobs; `fq priority <id> N`.
- `fq requeue <id>` reruns a finished job as a new one.
- Order work with dependencies, not polling: `--after afterok:<id>` or
  `--after afterok:<grp_...>` for a whole array or `--each` group.
  `--begin +2h` or `--begin 2026-09-24T09:00` defers the start.

## Long training

`--resume N` warns the job with SIGUSR1 five minutes before its walltime
(`--warn-signal`/`--warn-before` to change), resubmits it after timeout,
preemption or node failure, and gives every attempt `$FQ_CHECKPOINT_DIR`
(with `FQ_RESUMED=1` when it holds something). Save there on the signal and
load from it on start; the retry goes back to the machine that holds it.

## Interactive

`fq alloc --gpus 1 [--shell]` holds verified-idle GPUs on a workstation;
`fq shell <id> [-- CMD]` enters it with those GPUs pinned, and the shell ends
with the allocation. There are no interactive sessions on clusters.

## Exit codes

0 done and succeeded; 1 ended unsuccessfully; 2 refused or usage error (the
answer: do not resubmit it elsewhere); 3 not found; 4 wait timed out (the job
keeps running); 5 fleetqd unreachable; 6 auth; 7 server or version error;
8 rate limited; 130 interrupted (the job keeps running). With `--json`, stdout
is exactly one JSON object, errors included.

## Operating fleetqd

Upgrades, restores, tokens and target onboarding are in `fleetq/README.md`
and `fleetq/OPERATIONS.md` in the dotfiles repo. Every target starts disabled;
never enable one to get a job running.
