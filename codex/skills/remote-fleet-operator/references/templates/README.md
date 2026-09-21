# Slurm batch templates

Starting points for `fleetctl submit --native-batch <script> --target
<target>` on Slurm-backed sites. In native-batch mode the script's own
`#SBATCH` directives decide resources — fleetctl does not wrap or rewrite
the payload, so every directive here must be correct before submission; a
placeholder left in place is submitted exactly as written, not filled in
for you.

- `cpu.sbatch` — CPU-only job, single task.
- `gpu.sbatch` — single-node GPU job.
- `multi_gpu.sbatch` — multi-node GPU job (`--nodes` / `--ntasks-per-node`).
- `h200.sbatch` — H200-partition job; carries the extra `--qos` line an
  H200-class queue requires alongside `--account` (see
  [../kiac-cluster.md](../kiac-cluster.md) for why that combination exists).
- `array.sbatch` — job array; keep `%a` in the output/error filenames or
  concurrent array tasks overwrite each other's logs.

All templates use Slurm's own filename patterns in `--output`/`--error`:
`%j` (job ID), `%x` (job name), `%A` (array job ID), `%a` (array task ID).
Array jobs must keep `%a` — dropping it is the single most common way to
lose per-task logs.

Every `{{placeholder}}` (`{{partition}}`, `{{account_line}}`,
`{{gres_line}}`, `{{qos_line}}`, `{{mem}}`, `{{time}}`, and so on) is a gap
to fill from the target's real site facts, not from these examples. Run
`fleetctl queue list <target>` and `fleetctl protocol show <target>` first
and copy actual partition names, GPU GRES types, accounts, and QOS from
that output. Never copy a placeholder's surrounding example value blind,
and never invent a partition, account, QOS, or GRES type that has not been
confirmed live or declared in the target's protocol file.
