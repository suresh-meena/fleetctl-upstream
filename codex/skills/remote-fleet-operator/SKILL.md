---
name: remote-fleet-operator
description: Operate remote machines, pools, and project-bound compute environments through fleetctl. Use when the task involves running experiments on named machines, syncing repos to remote hosts, staging scripts, submitting jobs to schedulers, inspecting fleet config, importing SSH aliases, fanning a read-only check across the fleet, or migrating an older private inventory to the schema fleetctl reads today. Prefer this over ad hoc raw SSH whenever the host should come from the user's managed fleet.
---

# Remote Fleet Operator

Use `fleetctl` as the control plane for remote work.

## Goals

- Keep host and login data out of version-controlled repos.
- Reuse one global fleet inventory across projects.
- Avoid brittle shell quoting and repo-local SSH wrappers.
- Make remote actions boring: inspect, smoke test, sync, execute, submit.
- Keep sensitive connection material out of target metadata.
- Treat each target's `role` as the boundary on what may be done to it,
  and `--admin` as an acknowledgement rather than a convenience.
- Put each host's base `workdir` on the target itself.
- Use project bindings only for defaults and for project-specific absolute
  overrides when `workdir/<project-name>` is not correct.
- Encode site rules in reusable private protocol files under
  `~/.config/fleet/protocols.d/`, then bind targets to those protocols instead
  of teaching host-specific quirks inline.
- Prefer pass-backed fleet source data plus `fleetctl deploy-config` over
  hand-editing the generated `~/.config/fleet/` tree.

## First checks

Run these before changing anything substantial:

```bash
fleetctl doctor
fleetctl list
fleetctl project show
fleetctl explain <target-or-pool>
```

If the current project is not bound, inspect the available targets or pools and
either use an explicit target or bind the project.

The manual is in the tool: `fleetctl help <topic>` for overview, verbs, roles,
reach, config, secrets, jobs and examples, and `fleetctl doctor --probe` when
the live fleet itself is in question. Read the topic instead of guessing.

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

## Preferred workflow

1. Resolve context.
   Use `fleetctl project show`, `fleetctl resolve`, or `fleetctl show`.
2. Inspect role and protocol.
   Use `fleetctl explain <target>` for the admission answer, and
   `fleetctl protocol show` to decide whether the target is direct or
   scheduler-backed and whether `native_batch_required` forces site-native
   batch scripts. Use `fleetctl queue list` when queue choice matters.
3. Verify transport.
   Run `fleetctl smoke <target-or-pool>` unless the user explicitly wants a
   direct attempt first.
4. Sync code when needed.
   Use `fleetctl sync push ...`.
5. Run the work through the narrowest interface that fits:
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
6. Inspect jobs with `fleetctl job status`, `fleetctl job logs`, and
   `fleetctl job cancel`.

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
- For a host whose address changes on every start, use
  `secret_backend = "command"` with `secret_ref` naming a script that prints
  `host = "..."`. Its output is a secret: never echo it back to the user.
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

## Transport rules

- Prefer `fleetctl` over raw `ssh` for any host that belongs in the managed
  fleet.
- Prefer `fleetctl exec` over `ssh <host> 'bash -lc ...'` when the user payload
  is naturally an argv vector on direct hosts.
- On a `login` role, treat `fleetctl exec --admin` as control-plane access
  only. Use `fleetctl submit` for compute.
- A refusal is the answer, not an obstacle. When `fleetctl` exits 2 it names the
  alternative; take that path, or ask. Do not reach for raw `ssh` to do the
  thing that was just refused, and do not add `--admin` to make a refusal go
  away -- it only lifts the cells the role marks as needing acknowledgement.
- Container invocation belongs in a profile's `interpreter` or `submit_command`,
  for example `["apptainer", "exec", ...]`. There is no second runtime dimension:
  a protocol's `job_runtime` no longer exists, and a config still carrying it is
  reported by `doctor` and stripped by `fleetctl migrate-config`.
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
  unchanged.
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
