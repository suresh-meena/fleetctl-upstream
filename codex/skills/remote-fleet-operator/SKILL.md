---
name: remote-fleet-operator
description: Run and manage work on the user's own machines and clusters - the GPU workstations, the numpi bridge, and the Slurm sites KIAC and AMD MI210 - through fleetctl, the fq job queue (fleetqd) and fleetmon monitoring. Use whenever the user asks which GPUs are free, idle, busy or available, or whether all N GPUs are in use; to start, queue, spread, check, stop or cancel jobs, runs, sweeps or seeds on any machine; for job logs, queue length or why a job is waiting; to copy checkpoints or files between machines; to run a command on a named host; to write or fix Slurm batch scripts or choose partitions, GPUs, accounts or QOS; or to change fleet inventory, roles or protocols. Load it before any ssh, nvidia-smi, squeue, sacct, scancel or rsync aimed at those machines, and load it again after a context compaction.
---

# Remote Fleet Operator

Three tools drive one fleet, and each answers a different question:

- **`fq`** (client of `fleetqd`, the scheduler on numpi): run work and follow
  it, and read cached GPU usage. [references/fq.md](references/fq.md)
- **fleetmon** (read-only telemetry hub on numpi, port 8088): what every
  host and GPU is doing. [references/fleetmon.md](references/fleetmon.md)
- **`fleetctl`** (transport and admission policy): run a command, move
  files, submit to Slurm, inspect inventory. `fleetctl help <topic>` is its
  manual (overview, verbs, roles, reach, capabilities, config, secrets, jobs,
  examples).

## Route the request first

| The user asks | Start with | Never |
| --- | --- | --- |
| which GPUs are free or idle, are all N in use, what do I have | `fq usage --idle`, then `fq usage` | `fleetctl exec <host> -- nvidia-smi` host by host |
| start, queue or spread runs or seeds, use all idle GPUs | `fq submit --gpus N --on a,b,c` with `--array` or `--each` | `nohup`/`setsid` worker loops via `fleetctl exec` |
| is it done, show logs, why is it waiting | `fq wait`, `fq logs`, `fq explain`, `fq q` | `sleep; check` loops, `tail -f` over ssh |
| stop or cancel jobs | `fq cancel <id>` or `fq cancel --name 'pat*'`; for Slurm jobs outside fq, `fleetctl job cancel` | `kill <slurm-jobid>`, or killing processes you did not start |
| what is the queue or availability on KIAC or AMD | `fleetctl queue list <site>`, then **one** `fleetctl exec --admin --op-class monitor <site> -- sinfo ...` or `squeue ...` | repeated `squeue`, `sinfo` or `sacct` |
| run a Slurm job | `fleetctl preflight`, then `fleetctl submit` (or `fq submit --on <site>` when enabled) | `sbatch` over raw ssh |
| copy checkpoints or files between machines | `fleetctl sync transfer <src>:<path> <dst>:<path>` (`--admin` when an end is the bridge) | `scp`, or `rsync` over raw ssh |
| run a command on a machine | `fleetctl exec <target> -- <argv>` | raw `ssh host 'bash -lc ...'` |
| a machine named by IP, by "the one ending in 66", or loosely | `fleetctl list`, then `fleetctl show <target>` | guessing a target |
| health of the fleet, the hub or the queue | `fleetctl doctor`, `fq --json whoami`, the fleetmon dashboard | restarting services to see what happens |

`fq` reads (`usage`, `q`, `nodes`, `history`, `logs`, `explain`) come from
numpi's caches and contact no machine, so repeat them freely. Everything that
reaches a host goes through `fleetctl`.

Before the first `fq` call, check that the client works here:
`fq --json whoami`. Exit 0 means use `fq`. Otherwise follow the fallback table
in [references/fq.md](references/fq.md); do not improvise job control.

## Failures seen in real sessions

These are what went wrong when this skill was not loaded. Do not repeat them.

- Dozens of `nvidia-smi` calls across hosts to find a free GPU. One
  `fq usage --idle` answers it, using fleetmon's freshness rules.
- Hand-rolled GPU queues: a `worker.sh` per GPU launched with `setsid nohup`
  over `fleetctl exec`. Nothing tracked, retried or cleaned them up, and they
  could not be cancelled as jobs. Submit an `fq` array instead.
- Polling `squeue` on a login node every few minutes. It spends the site's
  control budget and admins can see it. Block on `fq wait`, or poll
  `fleetctl job status` at the site's approved cadence.
- Reporting a GPU as free from one sample, or from a stale one. Only
  `idle` in `fq usage` is free; `?` is unknown.

## First checks

Run these before changing anything substantial:

```bash
fq --json whoami                 # is the queue usable from here?
fleetctl doctor
fleetctl list
fleetctl project show
fleetctl explain <target-or-pool>
```

If the current project is not bound, inspect the available targets or pools and
either use an explicit target or bind the project. Run `fleetctl doctor --probe`
when the live fleet itself is in question. Read the manual topic instead of
guessing.

When a target may be scheduler-backed or otherwise policy-constrained, inspect:

```bash
fleetctl protocol show <target-or-pool>
fleetctl queue list <target-or-pool>
```

## Site context

For the user-provided **AMD GPU Cluster / MI210 Slurm site**, read
[references/amd-gpu-cluster.md](references/amd-gpu-cluster.md) before preparing
jobs or changing its protocol or queue configuration. It captures the supplied
`AMD Server.pdf`, including mandatory Slurm submission, GPU-only queue usage,
storage limits, and errors in the example scripts. Apply these site rules only
when the user or private inventory identifies this cluster; an AMD GPU alone
does not identify the site. The document's hardware and software descriptions
are source context, not live availability or permission to change inventory.

For the user-provided **KIAC GPU cluster** (NVIDIA
A5000/A6000/A100/ADA6000/H200, Slurm with accounts and QOS), read
[references/kiac-cluster.md](references/kiac-cluster.md) before preparing
jobs or changing its protocol, queue, or account/QOS configuration. It is
the audit trail for facts now enforced through
`~/.config/fleet/protocols.d/kiac.toml` — partition-to-GPU mapping, the
account/QOS matrix, and the manual's documented self-contradictions and
wrong examples — not the source fleetctl reads at runtime. Apply these
site rules only when the user or private inventory identifies this
cluster.

For Slurm syntax questions on either scheduler-backed site, prefer
[references/slurm-sources.md](references/slurm-sources.md) — authoritative
SchedMD pages — over general tutorials.

## Scheduler sites

Before submitting to any Slurm-backed target, check the script:
`fleetctl preflight <script> --target <target> [--live] [--strict] [--json]`.
`fleetctl submit` runs the same preflight automatically and refuses to
submit on ERROR — treat that refusal as the answer, not an obstacle to
route around.

- Without `--live` it is entirely offline, which is why the account and QOS
  matrix has to be declared on the queue for it to be checked at all. That
  declaration is not redundant with asking the cluster: `--live` runs
  `sbatch --test-only`, which validates syntax and allocation shape only. It
  does **not** enforce account, partition, or QOS policy — a job can pass the
  dry run cleanly and then pend forever once the scheduler applies its
  association matrix (verified on KIAC, 2026-09-14). A green preflight,
  `--live` or not, is necessary and never sufficient.
- `--live` reaches the host, so it is admitted as `submit` is: where
  submitting is refused, asking whether a submission would be accepted is
  refused too. It stages a copy, dry-runs it, removes the copy, and submits
  nothing. It is skipped when the offline checks already failed.
- After any newly granted account/partition/QOS combination, validate it
  with a real 5-minute smoke job before relying on it for real work —
  `--test-only` and preflight cannot substitute for a run that actually
  queues.
- Never invent a partition, GPU GRES type, account, QOS, module version,
  memory limit, or node mapping. Discover it from `fleetctl queue list` or
  `fleetctl protocol show`, or refuse.
- Memory units are K/M/G/T: write `--mem=16G`, never `--mem=16GB`.
- All `#SBATCH` directives must precede the first executable line in the
  script; sbatch silently ignores anything that comes after.
- Cancel with `scancel <jobid>` (or `fleetctl job cancel`), never
  `kill <jobid>` — `kill` signals a Unix process, not a Slurm job.
- Filesystem findings from a linter are host-relative: a path error found
  while checking off-cluster may only mean the script belongs on the login
  node, not that the path is wrong. Re-check there before treating it as a
  script bug.
- Starting points for native Slurm batch scripts live under
  [references/templates/](references/templates/) — fill their placeholders
  from `fleetctl queue list`, never copy the example values blind.
- Cluster availability is not in `fq usage`: fleetmon cannot see inside
  Slurm compute nodes. Ask the scheduler once per question, through
  `fleetctl exec --admin --op-class monitor`, and report what it said with
  the time you asked.

## Evidence discipline

Fleet and cluster work fails quietly: schedulers accept requests they will
never run, and a green dry run proves nothing about policy or execution.
When making, recording, or handing off claims about fleet or cluster
infrastructure, class every claim by its evidence — and keep the classes
separate:

| Class | Meaning | Example |
| --- | --- | --- |
| `verified-by-run` | a real job exercised it — cite job ID and state (`sacct` / `fleetctl job status` / `fq show`) | job 58822 COMPLETED with `chiru`+`h200_qos` on h200 |
| `verified-live` | read from the scheduler or fleet this session | `fleetctl queue list` MaxTime, `fq usage` sample age 4s |
| `changed-untested` | a code/config change with no run evidence yet | pinning `pyarrow==25.0.1` |
| `inferred` | reasoning only | "the array limit is probably MaxArraySize" |

Rules that follow:

- **"Applied" is not "verified."** A changed line, a pinned version, or new
  launch instructions prove nothing until a job runs. Never let
  `changed-untested` items appear under a "fixed" heading.
- **Do not stack fixes on unverified fixes.** Validate each layer with the
  cheapest real run (a 5-minute smoke job) before building the next change
  on it; otherwise a later failure cannot be attributed, and an earlier
  "fix" may itself be the bug.
- **Prefer enforced fixes over instructions.** A preflight ERROR or a
  protocol default cannot be silently ignored; a comment, README line, or
  "launch instruction" can. When a fix can be enforced by fleetctl's config
  or protocol declarations, enforce it there instead of documenting it.
- **Cite job IDs, not adjectives.** "Fixed the H200 wiring" carries no
  evidence; "job 58822, COMPLETED, account chiru, qos h200_qos" does.
- **Answer availability questions with numbers and their age.** "rtx4090:
  GPU 0 idle, GPU 1 busy (samples 6s old)" is an answer. "Looks mostly free"
  is not.

## Running work

When `fq` is usable and the destination is enabled in fleetqd, compute goes
through `fq`. fleetqd owns placement, retries, logs and the budgeted observer
for each managed Slurm site; an agent blocks on `fq wait` rather than polling.

```bash
fq usage --idle
fq --json submit --idempotency-key "<task>-<config-hash>" --gpus 1 --on <a>,<b> \
   --array 0-7%4 -- python train.py          # each task reads $FQ_ARRAY_TASK_ID
fq --json wait <job-id> --timeout 9m         # exit 4: still running, call again
fq logs <job-id> --tail 4000
```

- Derive the idempotency key from the task; on exit 5 retry with the *same*
  key. A refusal (exit 2) is the answer: do not resubmit it elsewhere.
- Read `job.terminal`, `job.success`, `job.execution.outcome` and
  `job.execution.exit.code` from the JSON, not the exit code alone.
- Everything else (dependencies, `--resume` checkpointing, spill to a
  cluster, `fq alloc`/`fq shell`, `modify`/`top`/`requeue`, exit codes) is
  in [references/fq.md](references/fq.md).

Without `fq` (not deployed here, no token, or the destination not enabled),
use `fleetctl` only where the site permits that workflow:

1. Resolve context with `fleetctl project show`, `fleetctl resolve`, or
   `fleetctl show`.
2. Use `fleetctl explain <target>` for the admission answer, and
   `fleetctl protocol show` to decide whether the target is direct or
   scheduler-backed and whether `native_batch_required` forces site-native
   batch scripts. Use `fleetctl queue list` when queue choice matters.
3. Run `fleetctl smoke <target-or-pool>` unless the user explicitly wants a
   direct attempt first.
4. Sync code when needed with `fleetctl sync push ...`.
5. On a scheduler-backed target, run `fleetctl preflight <script> --target
   <target>` first. `submit` runs it anyway, but running it first separates a
   script problem from a transport or policy one.
6. Run the work through the narrowest interface that fits:
   - `fleetctl exec <target> -- ...` for direct-host argv execution
   - `fleetctl exec --admin <target> -- ...` for control-plane commands such
     as `squeue` or `sinfo` on a login surface, never for compute
   - `fleetctl script <local-script> --target <target> -- ...` for staged
     scripts on direct hosts
   - `fleetctl submit <local-script> --target <target>` for compute jobs,
     especially when the resolved protocol is scheduler-backed
   - `fleetctl submit --native-batch <local-script> --target <target>` when a
     site expects a fully native scheduler script and `fleetctl` should not wrap
     the payload
7. Follow jobs with `fleetctl jobs`, `fleetctl job status`, `fleetctl job
   logs`, and `fleetctl job cancel`, within the polling limits below.

Poll at the site's approved cadence and within the configured control budget.
Never loop `squeue`, `sacct` or `fleetctl job status` to chase a faster
response.

- On a site with a `[control_budget]`, every control call asks the central
  authority for a permit. Exit 75 means it was denied (or unreachable): wait the
  `retry_after` it reports, when it gives one (under `--json`, in
  `details.retry_after`); never retry in a loop. Say what a read-only scheduler
  query is with `fleetctl exec --admin --op-class monitor -- squeue ...`.
- `--json` on `exec`, `script`, `sync`, `submit` and `job status|cancel` prints
  one `fleetctl.result/v1` document. Branch on `outcome` and
  `may_have_executed`, not on exit codes. When a `submit` says
  `may_have_executed: true`, preserve the request and reconcile the exact
  receipt, scheduler identity and site evidence within budget. A job name is
  only a lookup label; never infer that an empty name search proves nonexecution
  or submit the same attempt again.
- `--timeout <seconds>` bounds the whole command, staging included; it exits
  124. A timeout after the command was sent is `may_have_executed: true`.

## Configuration tasks

When the user wants to bring hosts into the fleet:

- Import an existing SSH alias with `fleetctl import-ssh <alias> --role
  <bridge|login|compute|workstation|storage>`. The role is the whole point
  of the import; guessing it wrong is how a login node gets used as a
  workstation.
- Define reusable protocols in `~/.config/fleet/protocols.d/*.toml` and point
  targets at them with `protocol = "name"`.
- Declare how a host is reached on the target itself, direct first:
  `reach = ["direct", "via:<bridge>"]`. Never hide a hop in a secret or in
  `~/.ssh/config`.
- Seed the current live fleet into pass with `fleetctl seed-pass`, then rebuild
  the generated tree with `fleetctl deploy-config`.
- When the dotfiles repo is the source of the operator surface, prefer the
  usual `./setup.sh` flow to install `fleetctl` and regenerate `~/.config/fleet`
  automatically when the `infra/fleet` pass prefix is present.
- Bind a project with `fleetctl project bind <path> --target <name>` or
  `--pool <name>`.
- When one project needs an absolute path that does not follow
  `workdir/<project-name>` on some hosts, use repeated
  `--target-root target=/remote/path` entries on the project binding.
- A host added to fleetctl is not yet monitored or schedulable. Fleetmon needs
  its helper (`scripts/install-helper`), and fleetqd needs an enabled `[[node]]`.
  Both are deliberate, per-target steps; see
  [references/fleetmon.md](references/fleetmon.md) and
  [references/fq.md](references/fq.md).

## Transport rules

- Prefer `fleetctl` over raw `ssh` for any host that belongs in the managed
  fleet.
- Prefer `fleetctl exec` over `ssh <host> 'bash -lc ...'` when the user payload
  is naturally an argv vector on direct hosts.
- On a `login` role, treat `fleetctl exec --admin` as control-plane access
  only. Use `fleetctl submit` or `fq` for compute.
- A refusal is the answer, not an obstacle. When `fleetctl` exits 2 it names the
  alternative; take that path, or ask. Do not reach for raw `ssh` to do the
  thing that was just refused, and do not add `--admin` to make a refusal go
  away -- it only lifts the cells the role marks as needing acknowledgement.
- Container invocation for submitted work belongs in a profile's `interpreter`.
  A Slurm profile's `submit_command` must call `sbatch` directly and pass
  `{script}` exactly once as its final argument; a wrapper could run work on the
  login node before Slurm receives it. There is no second runtime dimension: a
  protocol's `job_runtime` no longer exists, and a config still carrying it
  fails to load as an unknown key.
- Ask the whole fleet a read-only question with `--all` or `--tag <tag>`, and
  `--jobs <n>` to overlap them: `fleetctl smoke --all --jobs 4`. Only `smoke`,
  `exec` and `job status` fan out; `sync`, `submit`, `script` and `job
  logs|cancel` refuse and say why. Never loop a refused verb over `fleetctl
  list` to fake it -- that is the partial-failure state the refusal exists to
  prevent, and for `submit` it is a double submission.
- Read the `note:` lines a fan-out prints. They name what the run did not cover,
  and a fan-out that covered eight of ten hosts otherwise reads as ten.
- `fleetctl probe <target>` measures a host and caches the facts for `doctor` to
  compare against the declaration. It is advisory by construction: a probed fact
  never selects a target or admits a verb. Fix drift by correcting the
  inventory, not by expecting the measurement to win.
- When a protocol reports `native_batch_required = true`, use
  `fleetctl submit --native-batch` so the scheduler sees the site-native script
  unchanged. That path submits the script exactly as written, so the preflight
  is the only thing between a bad directive and the queue.
- `--no-preflight` is an acknowledgement, not a convenience, and it has the
  same shape as `--admin`: it silences a check, it does not make the scheduler
  more permissive. Fix what the diagnostic names, or ask. A rule ID
  (`KIAC023`, `SLURM020`) is stable enough to argue with specifically.
- Prefer `fleetctl script` or `fleetctl submit` when the command is large enough
  that quoting would become fragile.
- Routing is declared, not guessed: `reach` tries direct first and falls back to
  a `via:` bridge once per control socket. `--route` pins a declared route and
  `--no-fallback` refuses to degrade. A failing command is never re-routed, so
  never wrap `exec` or `submit` in a retry loop; `sbatch` would run twice.
- Select on declared capabilities rather than probing: `--require 'vram_gb>=24'`
  refuses a named target that does not match, and `--fit` picks the smallest
  sufficient candidate instead. Neither moves work to a host you did not name.
- Use `--dry-run` before a first-time submit and before any `sync --delete`. It
  resolves the plan locally and claims nothing about remote state -- for
  `submit` it prints the rendered batch script, and for `sync` it is a real
  `rsync --dry-run`.
- Use raw `ssh` only for transport debugging or when `fleetctl` genuinely lacks
  the needed primitive.

## Privacy rules

- Do not write sensitive host or login information into repo files.
- Keep secrets under `~/.config/fleet/secrets/` or the configured secret
  backend.
- When reporting fleet state back to the user, avoid echoing sensitive host
  details unless they explicitly asked for them.
