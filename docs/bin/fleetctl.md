# fleetctl

Run work on machines you do not all administer, through a private inventory,
under one admission policy. One file of Python 3 and the standard library
driving `ssh`, `scp` and `rsync` — no daemon, nothing installed on the remote.

`fleetctl help` is the manual: `overview verbs roles reach capabilities config
secrets jobs examples`. This page is the tour.

For the supplied AMD MI210 cluster, the operator skill includes
[AMD GPU cluster site context](../../codex/skills/remote-fleet-operator/references/amd-gpu-cluster.md)
from `AMD Server.pdf`: Slurm policy, storage limits, hardware background, and
corrections needed before using its example scripts.

## Why

The fleet grows in variety faster than in size — a Raspberry Pi whose only job
is to be jumped through, a bare-metal GPU box behind it, a Slurm site reached
through a login node. So most of fleetctl's value is in what it refuses.
Running `python train.py` on a login node is not a mistake it lets you make by
accident.

## The pieces

- **Target** — one SSH-reachable surface. A Slurm compute node is *not* a
  target; node types are queue presets on the site's protocol.
- **Pool** — an ordered set of targets. Selection takes the first enabled one.
- **Protocol** — a site: whether compute is scheduled there, and its queues.
- **Profile** — how work runs: interpreter, and submit/status/logs/cancel.
- **Project binding** — a local directory mapped to a target and a remote root,
  so a bound directory needs no `--target`.
- **Secret** — connection material, kept out of the inventory and out of git.

| path | holds |
| --- | --- |
| `~/.config/fleet/` | `targets.d/` `pools.d/` `protocols.d/` `profiles.d/` `secrets/` `config.toml` `projects.toml` |
| `~/.local/state/fleet/` | private `known_hosts`, `routes.json`, `jobs.jsonl`, `audit.jsonl`, probed `facts/` |
| `~/.cache/fleet/mux/` | SSH control sockets |

`$FLEET_CONFIG_HOME`, `$FLEET_STATE_HOME` and `$FLEET_CACHE_HOME` move all
three; `--config-home`, `--state-home` and `--cache-home` do it for one run.

An unknown key in any of those files is fatal, and the error names the file, the
key and a likely correction — because the default that fills in for a
misspelling is always the permissive one.

## Roles decide what may be done

Every target declares a `role`. A role is a taint, not a label: it repels work,
and `--admin` is the toleration — permission, never a recommendation.

```
role         ssh   exec  script sync  submit job
bridge       yes   admin no     admin no     no
login        yes   admin admin  yes   yes    yes
compute      yes   yes   yes    yes   yes    yes
workstation  yes   yes   yes    yes   yes    no
storage      yes   admin no     yes   no     no

yes = allowed    admin = needs --admin    no = refused
```

`fleetctl help roles` prints that table, generated from the same constant the
admission check reads, so the two cannot drift apart.

There is deliberately no role meaning "scheduler node where direct execution is
fine", so the state this tool exists to prevent cannot be spelled in the config
at all. A refusal names the alternative — *run compute through `fleetctl
submit`* — and no flag lifts it. `fleetctl explain <target>` reports every cell
for one host, where a refusal is the answer rather than an error.

Role and protocol must agree, checked at load: `role = "login"` on a protocol
that runs work directly is refused, and so is any other role on a
scheduler-backed one. A target with no `role` gets the conservative reading of
its protocol, and `doctor` names it.

## Reach: direct first, the bridge as fallback

How a host is reached is inventory, not a credential.

```toml
# targets.d/rtx1.toml
reach = ["direct", "via:numpi"]
```

Ordered, and the order is the point: direct is the route taken, the bridge is
the declared fallback. Every `via:` must name an enabled target whose `role` is
`bridge`; self-bridges and cycles are refused at load, once per fleet rather
than once per dependent.

A hop is a full fleetctl connection to the bridge — its own secret, and the same
private `known_hosts`, `IdentitiesOnly`, timeouts and control socket the
destination gets — so it inherits nothing from `~/.ssh/config`, and it chains.
Using a bridge is transport, not execution, so it takes no admission check;
`exec` on that same bridge is still refused. A password-authenticated bridge is
refused as a hop, because sshpass hands one password to the whole process tree.

`--route direct` or `--route via:<bridge>` pins a *declared* route; fleetctl
will not invent one. `fleetctl list --format topology` prints the graph.

Fallback is automatic, and happens once per control socket rather than once per
command: a warm master means the route already works, and a cold one is filled
by trying each route in declared order until a master comes up. Direct gets 5
seconds and a bridge 10, so preferring direct costs one timeout per
`ControlPersist` window, and only when direct is down. Each route gets its own
socket, since `%C` does not hash the ProxyCommand. `--no-fallback` refuses to
degrade, and says so when it fails rather than telling you to declare the
fallback you suppressed.

A route that cannot be attempted counts as a route that did not come up — a
wedged master that will not answer `ssh -O check`, a socket path over the length
limit — so the next route still gets its turn. A `via:` route brings the
bridge's own master up first, over the bridge's own `reach`, so a bridge with a
fallback can use it.

Once a master exists a failing command is a failing command — never retried,
never re-routed. That is what keeps `exec` and `submit` at-most-once: a
re-routed `sbatch` would queue the job twice.

## Capabilities: ask for hardware, not for a hostname

Declare what a machine has and let the tool choose:

```toml
# targets.d/rtx1.toml
[capabilities]
arch      = "x86_64"
gpu_model = "RTX 3090"
gpu_count = 1
vram_gb   = 24
cpus      = 16
mem_gb    = 64
```

```
fleetctl list --format capabilities                        # one row per surface
fleetctl resolve --require 'vram_gb>=24' --fit             # smallest that fits
fleetctl submit train.sh --require 'vram_gb>=40' --fit
```

Any key you like; `gpu_count`, `vram_gb`, `cpus` and `mem_gb` are compared as
numbers. Operators: `>= <= > < = == !=`, comma-separated or repeated. An
undeclared capability never satisfies a requirement.

A requirement narrows a choice; it never moves work. Name a target — or let a
project binding name one — and `--require` is a check that refuses with the
mismatch. Give it a pool, or nothing at all, and it is a filter; `--fit` then
takes the smallest sufficient candidate (fewest GPUs, then VRAM, CPUs, memory,
then by name) and an ambiguous match without `--fit` is refused rather than
guessed.

Declared values are authoritative and nothing measured ever selects anything.
`fleetctl probe <target>` records what a host reports — `nvidia-smi`, `lscpu`,
`free`, `python3`, and `sinfo` for the queues at a scheduler site — into
`<state-home>/facts/<target>.json` at `0600`, and the only thing that reads it
is `doctor`, which reports the difference. Measuring short of a declaration is a
problem, since a box with less than it promised accepts a job sized for the
promise and then fails it; measuring over it is a warning that the declaration
is stale. The machine being described is the one supplying the description, which
is why it never gets a vote.

On a Slurm site the hardware belongs to the partition, so a queue carries its
own `[queue.capabilities]`. Those layer over the target's, `--require` filters
the partitions, and `--fit` picks the smallest sufficient one. `--gpus` and
`--cpus-per-task` are checked against the declared numbers before anything
connects. `fleetctl help capabilities` has the rest.

## Start

```bash
fleetctl init
fleetctl import-ssh <alias> --name <target> --role workstation
fleetctl doctor            # must be clean before anything else
fleetctl doctor --probe    # also checks remote python3, which exec needs
                          # (a host out of reach is a warning, not a problem)
fleetctl probe <target>    # measure one host; doctor then reports the drift
```

`migrate-config` rewrites an older inventory in place: it fills in `role`, makes
`protocol` explicit, lifts a secret's `proxy_jump` into `reach` with `direct` in
front of it, and strips the keys nothing reads any more — `transport`,
`ssh_host_alias`, `metadata`, `job_runtime` and a pool's `strategy`. Those stay
*accepted* so an unmigrated inventory still loads, and `doctor` names every file
still carrying one.

## Daily

```bash
fleetctl smoke <target>                            # transport, then workdir
fleetctl exec <target> -- python3 -c 'print(1)'    # after `--` is verbatim
fleetctl script ./setup.sh --target <target> -- --flag value
fleetctl sync push .                               # from a bound directory
fleetctl submit train.sh --target <target> --queue <preset>
fleetctl jobs                                      # what you have submitted
fleetctl job status <id>                           # target from the ledger
```

One question, whole fleet:

```bash
fleetctl smoke --all --jobs 4
fleetctl exec --tag gpu --jobs 4 -- nvidia-smi -L
fleetctl job status <id> --all
```

Sequential without `--jobs`. Each host's output is prefixed with its name and
held until every host has answered, then one line per target says `ok`,
`failed`, `refused` or `plan`; exit is 2 if anything was refused, otherwise the
first non-zero remote code by target name.

Only `smoke`, `exec` and `job status` fan out. `sync`, `submit`, `script`,
`ssh`, `probe` and `job logs|cancel` refuse by name and say why: they write, or
they mean something different at each site, and partial failure across a fleet
leaves a state nothing afterwards can report. A fanned-out `sbatch` is a
fanned-out double submission.

Concurrency is capped at four per reach-hop rather than globally — a bridge
multiplexes every connection through one small machine, and ClusterShell's
default fan-out of 64 is a number for a homogeneous LAN. A target that declares
a `via:` fallback counts against that bridge even when direct is up; `--route
direct --no-fallback` narrows the route list and gets the full `--jobs` back.
Anything left out of the run — a disabled host, a cap, a target that would not
resolve — is printed as a `note:` rather than passed over.

`--dry-run` prints the fully resolved plan and exits 0, claiming nothing about
remote state — except for `sync`, where it runs `rsync --dry-run
--itemize-changes` and returns rsync's own code.

`sync --delete` refuses a home or root directory outright, and refuses the
target's own workdir, or any pull, without `--force`.

`sync` compresses at level 1 rather than rsync's default 6, because a transfer
multiplexed through a Raspberry Pi is bound by the compressor, not the link.
`--compress-level 0` turns compression off for a fast local hop.

`submit` wraps your script in a scheduler preamble built from the queue preset;
`--native-batch` sends your file unchanged instead. Nothing is submitted unless
a job id could be reported back.

## What gets written down

Every accepted submission appends one line to `$FLEET_STATE_HOME/jobs.jsonl`,
after the job id has been printed — so recording a job can never be what loses
one. `fleetctl jobs` lists them newest first, and `job status|logs|cancel` read
them, so an id the ledger knows needs neither `--target` nor `--profile`. An
explicit flag still wins; an id two targets both claim is refused rather than
guessed.

The nine subcommands that open a connection each append one line to
`$FLEET_STATE_HOME/audit.jsonl`: time, subcommand, resolved target and role,
route taken, the flag names you typed, and the exit code — refusals included,
since a refused `exec` on a login node is the line most worth having. No flag
value and nothing after `--` is recorded, because that is where a pasted token
would be; you cannot replay a command from this file, and that is the point.

Both are `0600` and keep their newest 2000 lines. Staged scripts under a
profile's `remote_script_root` are collected after 14 days, by the same round
trip that creates the next one.

## Secrets

`host`, `user`, `port`, `auth`, `identity_file`, `password` and the SSH tuning
knobs live in a secret, never in a target file — putting one in a target is a
refusal with that explanation. Three backends: a private TOML file, a `pass`
entry, an inline table, or a command fleetctl runs. Resolution is lazy.

`secret_backend = "command"` runs `secret_ref` and reads the same TOML off its
stdout, merged over the target's inline `[secret]` table — for the field that
will not hold still, such as a cloud box whose address changes on every start.
Cached 30 seconds in memory only, never on disk. Its stdout is a secret: it is
redacted like any other, and a failing upcall is a refusal naming the command
and its stderr rather than a half-resolved connection.

```bash
fleetctl show <target> --secret               # host and user both redacted
fleetctl show <target> --secret --sensitive   # ask for the real values
fleetctl seed-pass --pass-prefix infra/fleet
fleetctl deploy-config --pass-prefix infra/fleet --doctor
```

`deploy-config` renders a fresh tree beside the live one, validates it, and only
then swaps — keeping one previous generation, and never clearing `secrets/`
unless every target is pass-backed and it can refill them. `./setup.sh` runs it
when the `infra/fleet` pass prefix exists locally.

Files are written `0600` and directories `0700`; `doctor` treats a group- or
world-readable secret as a problem, not a warning. Prefer keys — and above all
on the bridge, which every other host is reached through.

## Exit status

`0` success · `1` `doctor` found problems · `2` any refusal, and any usage error
· `130` interrupted. Anything else is the remote command's own.

## Shell integration

`fssh` in Zsh merges classic SSH host discovery with `fleetctl list --format
ssh-hosts`; picking a `fleet:*` entry routes through `fleetctl ssh`.
