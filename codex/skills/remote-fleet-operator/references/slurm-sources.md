# Slurm sources: authoritative references

Prefer these over tutorials or general web answers when answering Slurm
syntax questions. `fleetctl preflight`'s offline rules for Slurm-backed
targets derive from the SchedMD pages below.

| Resource | Used for |
| --- | --- |
| [sbatch](https://slurm.schedmd.com/sbatch.html) | Directive parsing and first-executable-line rule, memory suffixes (K/M/G/T), filename patterns, `--test-only`, client RPC caution |
| [GRES guide](https://slurm.schedmd.com/gres.html) | `--gres=name[:type]:count` semantics; types are admin-configured |
| [CPU Management](https://slurm.schedmd.com/cpu_management.html) | nodes x tasks x cpus-per-task interaction |
| [sinfo](https://slurm.schedmd.com/sinfo.html) | Partition/node/GRES discovery (JSON where supported) |
| [scontrol](https://slurm.schedmd.com/scontrol.html) | Partition MaxTime, node details, job records |
| [squeue](https://slurm.schedmd.com/squeue.html) + [Job Reason Codes](https://slurm.schedmd.com/job_reason_codes.html) | Pending-job diagnostics (`Resources`, `Priority`, `Dependency`, association/QOS limits) |
| [sacct](https://slurm.schedmd.com/sacct.html) / [sacctmgr](https://slurm.schedmd.com/sacctmgr.html) | Finished-job history; account/QOS associations |
| [Job Arrays](https://slurm.schedmd.com/job_array.html) | `%A`/`%a` naming, array spec syntax |
| [kill(2) man page](https://man7.org/linux/man-pages/man2/kill.2.html) | Why `kill <jobid>` is wrong and `scancel` is right |

Version caveat: option sets and JSON output vary across Slurm releases —
see [amd-gpu-cluster.md](amd-gpu-cluster.md) for a site stuck on a release
that predates `--json`. Prefer structured output when a target's Slurm
version supports it and fall back to stable `scontrol` text otherwise.
