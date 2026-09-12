# AMD GPU cluster: site context for fleetctl

Source: the user-provided **AMD Server.pdf**, titled **AMD GPU Cluster**,
five pages. The original is retained locally at `sureshproblem/AMD Server.pdf`
in the parent dotfiles workspace and excluded from Git because it contains
private connection details. This reference is self-contained for skill installs;
private connection details and personal paths from the PDF are omitted.

Source SHA-256:
`87953a850d7702b7994281c0c1115288a15b74ebf07348b8c3f564d37465a4f8`.
Context extracted on 2026-09-08. Page numbers below refer to the PDF page order.
These are documented facts and policies, not a report of current cluster state.

## Site rules

The PDF makes two requirements explicit (p. 3):

- Submit all computational jobs through Slurm. Running jobs directly on the
  servers is prohibited. SSH access does not authorize direct compute.
- GPU queues are for GPU workloads. CPU-only jobs must not occupy them.

Apply these requirements to this site, not to every AMD target or every fleet.
If its private configuration permits direct compute, resolve that conflict
before running a workload; do not use a permissive setting to bypass the rule.

## Documented hardware and software

The table on p. 1 lists a management node, a storage node, and three GPU nodes.
Each GPU node is described as follows:

| Property | Documented value |
| --- | --- |
| GPU | Four AMD MI210 devices |
| CPU | AMD EPYC 9654; 96 cores; a separate “Processor” field says 192 |
| System RAM | 1.5 TB |
| Storage | 7 TB × 2 |

The three GPU rows total 12 devices across the cluster; that is not a
single-job allocation limit. The PDF does not define the “Processor” field,
GPU memory capacity, GPU architecture identifier, or current allocatable
resources. The storage-node table has inconsistent processor/memory values;
do not use those values as inventory limits.

Page 2 lists Ubuntu 22.04, “ROCk drivers for MI210 GPUs 6.0.6,” Slurm 22.05.8,
and LDAP authentication. These version descriptions are historical context;
the PDF does not establish a complete ROCm/framework/container compatibility
matrix.

## Access and storage

Access is restricted to the institution's CSA students and faculty; student
accounts require advisor approval and an account-validity period. The access
instructions assume connectivity from the institutional network (p. 2).

Each user receives 20 GB in their home directory. Additional temporary storage
is cleaned weekly (p. 2). Choose project workdirs with that limit in mind and
copy results that must persist out of temporary storage before cleanup.
Resolve actual paths through private configuration.

## Scheduler examples and their limits

| PDF example | Resource request shown | Pages |
| --- | --- | --- |
| Single-node GPU job | Node-specific partition, `--ntasks=1`, `--gres=gpu:1`, `--time=00:05:00` | 3–4 |
| Example labeled multi-node | Partition `GPU`, `--ntasks=1`, `--gres=gpu:4`, `--time=00:05:00` | 4 |
| Python application | Partition `GPU`, `--cpus-per-task=48`, `--gres=gpu:1`, `--mem=256G` | 4–5 |

The application initializes Conda, activates an environment, and runs a
parameterized Python experiment. Its environment and project paths are personal
examples. The queue names and resource requests are examples, not confirmed
current partitions, site maxima, recommended defaults, or free capacity.
Accounts, QoS, reservations, and complete partition limits are not specified.

Correct these source issues when preparing work:

- `scancle` is a typo for `scancel` (p. 3); use `fleetctl job cancel <job-id>`
  for a tracked job.
- `nvidia-smi` in the first GPU example is inconsistent with AMD MI210 hardware
  (p. 3). The PDF supplies no validated AMD replacement command; determine
  supported AMD tooling from the site's configuration and runtime.
- The example labeled multi-node supplies no node count, and its
  `--ntasks=1` comment claims eight CPUs (p. 4). Neither establishes a
  multi-node allocation or an eight-CPU request.

## Applying this context through fleetctl

Use the existing target/protocol/profile model: the login surface is a target
with `role = "login"` and a Slurm protocol. Compute partitions belong in that
protocol's queue presets. Keep host mappings, accounts, routes, and workdirs in
private configuration rather than copying connection material from the PDF.
When maintaining declarations, put compute hardware on queue capabilities;
confirm the documented AMD vendor, MI210 model, and per-node GPU count against
the intended partition. Do not invent undeclared GPU memory or scheduling limits.

Resolve the actual target alias and inspect its local configuration:

```bash
fleetctl explain <target>
fleetctl protocol show <target>
fleetctl queue list <target>
```

For an authorized scheduler inspection, use the login target's control plane:

```bash
fleetctl exec --admin <target> -- sinfo
fleetctl exec --admin <target> -- squeue --me
```

Prepare work through `fleetctl submit`. When preserving a site-native batch
script, or when `native_batch_required = true`, inspect the script and its local
submission plan first:

```bash
fleetctl submit --native-batch ./job.sbatch --target <target> --dry-run
```

Native batch mode preserves the script's own `#SBATCH` directives and rejects
resource override flags. The current CLI does not validate those directives
against queue capabilities, so review partition, GPU request, CPUs, memory,
walltime, and environment setup in the script. A dry run does not establish
scheduler acceptance or availability. This context does not itself authorize
submission, installation, or changes to the live fleet.
