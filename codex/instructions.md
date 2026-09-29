## Remote Fleet

- Treat `fleetctl` as the default interface for remote hosts, experiments, and
  project sync.
- Prefer `fleetctl exec`, `fleetctl script`, `fleetctl sync`, and
  `fleetctl submit` over hand-written `ssh '...'` command strings.
- Before using an unfamiliar target, inspect its rules with
  `fleetctl protocol show <target>` and, for scheduler-backed targets,
  `fleetctl queue list <target>`.
- Container invocation for submitted work belongs in the profile's
  `interpreter`. A Slurm profile's `submit_command` must invoke `sbatch`
  directly, with `{script}` as its final argument; wrappers could run the job
  on the login node before Slurm sees it. There is no `job_runtime`: how work
  is invoked belongs to the profile that invokes it.
- If `fleetctl protocol show <target>` reports `native_batch_required = true`,
  use `fleetctl submit --native-batch` and keep the site-native scheduler
  directives in the submitted script instead of relying on the wrapper path.
- When a site expects a fully native batch script, use
  `fleetctl submit --native-batch <script> --target <target>` so the staged file
  is handed to the scheduler unchanged.
- Before running substantial remote work on a host or pool, start with
  `fleetctl smoke <target>` unless the user explicitly asks to skip it.
- When a project has a fleet binding, rely on that binding instead of inventing
  repo-local remote config.
- A target's `role` decides what may be done to it, and `fleetctl explain
  <target>` reports every verb for one host. On a `login` role,
  `fleetctl exec --admin <target> -- ...` is for control-plane commands such as
  `squeue`, `sinfo` and `sacct`; compute goes through `fleetctl submit`.
- A refusal is the answer, not an obstacle. `fleetctl` exits 2 and names the
  alternative; take it, or ask. Do not fall back to raw `ssh` to do the thing
  that was just refused.
- Routing is declared on the target, direct first with a `via:` bridge as the
  fallback, and chosen once per control socket. Never wrap `exec` or `submit` in
  a retry loop: a re-run of an accepted `sbatch` queues the job twice.
- The manual is in the tool: `fleetctl help <topic>` covers verbs, roles, reach,
  capabilities, config, secrets and jobs. Check yourself with `fleetctl explain`
  and `--dry-run` before acting, and `fleetctl doctor --probe` for the live fleet.
- Only read-only verbs fan out (`--all`, `--tag`, `--jobs`); `sync` and `submit`
  refuse, and looping them over `fleetctl list` recreates exactly the
  partial-failure state the refusal prevents. Read the `note:` lines: they name
  what a run did not cover.
- `fleetctl probe` measures a host; the declaration still decides. A probed fact
  never selects a target, so fix drift in the inventory rather than expecting the
  measurement to win.
- Keep site policy and queue defaults in private fleet protocol files under
  `~/.config/fleet/protocols.d/`, not in repo code or project-specific hacks.
- Treat `~/.config/fleet/` as generated state when the user has enabled the
  pass-backed deploy flow. Use `fleetctl deploy-config` instead of manually
  editing generated files, and seed or update the private source data in `pass`
  when persistent fleet metadata changes.
- The standard dotfiles `./setup.sh` flow deploys the generated fleet config
  automatically when the `infra/fleet` pass prefix is present, so prefer
  repairing the pass-backed source data over hand-editing `~/.config/fleet/`.
- Keep sensitive connection material out of target records. Host-level
  `workdir` values belong on targets, while project-specific remote-path
  overrides belong in `projects.toml` when `workdir/<project-name>` is not the
  right landing path.
- Keep sensitive hostnames, IPs, usernames, passwords, and key paths in the
  private fleet config under `~/.config/fleet`, never in version-controlled
  project files.
- Use raw `ssh` only when `fleetctl` cannot express the operation cleanly or
  when debugging the transport layer itself.
