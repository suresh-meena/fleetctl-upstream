"""The preflight gate in front of `submit`, driven through the real CLI.

These exist because the gate once shipped refusing correct submissions and
117 green tests did not notice: nothing drove `submit` through it.  Two ways
it was wrong, one test group each --

  * on the queue path it linted the payload, which never carries #SBATCH
    lines, instead of the wrapper that carries all of them, so every
    `submit --queue ...` was refused for having no --partition;
  * path checks were errors, so a job naming /storage/... was refused on a
    laptop where /storage does not exist.

Every run is `--dry-run` against a target at 192.0.2.1 (RFC 5737 TEST-NET-1,
unroutable by definition), with its own config, state and cache homes.  A
dry run never connects; the last assertion in each test checks that no
control socket appeared, so a regression that did connect would fail here
rather than reach a real host.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PROTOCOL_TOML = """\
version = 1
name = "kiac"
kind = "slurm"
default_profile = "slurm-batch"
rule_prefix = "KIAC"

[[queue]]
name = "short"
partition = "short"
default = true
gpus = 1
time_limit = "00:10:00"
account = "research"
max_time = "24:00:00"
evidence = "verified-live"
as_of = "2026-09-14"

[[queue]]
name = "a100"
partition = "a100"
gpus = 1
time_limit = "00:10:00"
account = "research"
max_time = "24:00:00"
allowed_accounts = ["research"]
denied_accounts = ["chiru"]
evidence = "verified-by-run"
as_of = "2026-09-14"

[[queue]]
name = "h200"
partition = "h200"
gpus = 1
time_limit = "00:10:00"
qos = "h200_qos"
max_time = "24:00:00"
required_qos = "h200_qos"
account_required = true
allowed_accounts = ["chiru"]
evidence = "verified-by-run"
as_of = "2026-09-14"
"""

PROFILE_TOML = """\
version = 1
name = "slurm-batch"
job_id_pattern = "Submitted batch job (?P<job_id>\\\\d+)"
submit_command = ["sbatch", "{script}"]
status_command = ["squeue", "--jobs", "{job_id}"]
cancel_command = ["scancel", "{job_id}"]
logs_command = ["sacct", "--jobs", "{job_id}"]
"""


@pytest.fixture()
def sandbox(tmp_path):
    """A fleet with one login target that cannot be reached."""
    # A unix socket path is capped near 100 bytes and tmp_path is long, so the
    # control path lives in a short directory of its own.
    sockets = Path(tempfile.mkdtemp(prefix="fc-", dir="/tmp"))
    config = tmp_path / "cfg"
    for sub in ("targets.d", "protocols.d", "profiles.d", "secrets"):
        (config / sub).mkdir(parents=True)
    (config / "config.toml").write_text(
        f'version = 1\n\n[ssh]\ncontrol_path = "{sockets}/%C"\n'
    )
    (config / "projects.toml").write_text("version = 1\n")
    (config / "protocols.d" / "kiac.toml").write_text(PROTOCOL_TOML)
    (config / "profiles.d" / "slurm-batch.toml").write_text(PROFILE_TOML)
    secret = config / "secrets" / "login.toml"
    secret.write_text('host = "192.0.2.1"\nuser = "nobody"\nport = 9\n')
    secret.chmod(0o600)
    (config / "targets.d" / "login.toml").write_text(
        'version = 1\nname = "login"\nrole = "login"\nprotocol = "kiac"\n'
        f'workdir = "/home/nobody"\nsecret_backend = "file"\nsecret_ref = "{secret}"\n'
    )
    yield tmp_path, config, sockets
    shutil.rmtree(sockets, ignore_errors=True)


def submit(fleetctl_path, sandbox, script: Path, *args: str):
    tmp_path, config, sockets = sandbox
    proc = subprocess.run(
        [
            sys.executable, str(fleetctl_path),
            "--config-home", str(config),
            "--state-home", str(tmp_path / "state"),
            "--cache-home", str(tmp_path / "cache"),
            "submit", str(script), "--target", "login", "--dry-run", *args,
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert not any(sockets.iterdir()), "a dry run opened a connection"
    return proc


@pytest.fixture()
def payload(tmp_path):
    script = tmp_path / "train.sh"
    script.write_text("#!/bin/bash\nset -euo pipefail\npython3 train.py\n")
    return script


# -- the queue path lints the wrapper, not the payload ------------------------


def test_queue_submit_is_not_refused_for_the_payload_lacking_directives(
    fleetctl_path, sandbox, payload
):
    proc = submit(fleetctl_path, sandbox, payload, "--queue", "a100")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "KIAC001" not in proc.stderr
    assert "as wrapped for queue 'a100'" in proc.stderr


def test_queue_submit_checks_what_the_overrides_produce(
    fleetctl_path, sandbox, payload
):
    """The wrapper is what sbatch reads, so a CLI override is checked too."""
    proc = submit(fleetctl_path, sandbox, payload, "--queue", "short", "--time", "72:00:00")
    assert proc.returncode == 2
    assert "KIAC033" in proc.stderr
    assert "nothing was submitted" in proc.stderr


def test_queue_without_its_required_account_is_refused(fleetctl_path, sandbox, payload):
    """h200 deliberately presets no account; the real job would pend forever."""
    proc = submit(fleetctl_path, sandbox, payload, "--queue", "h200")
    assert proc.returncode == 2
    assert "KIAC020" in proc.stderr


def test_wrapper_paths_are_not_reported_as_the_operators(fleetctl_path, sandbox, payload):
    """`cd /home/nobody` and the staged script path are fleetctl's own lines."""
    proc = submit(fleetctl_path, sandbox, payload, "--queue", "a100")
    assert "FS001" not in proc.stderr and "FS002" not in proc.stderr


@pytest.mark.parametrize("json_flag", [False, True])
def test_slurm_profile_cannot_run_script_directly_on_login(
    fleetctl_path, sandbox, payload, json_flag
):
    """Reject a non-scheduler submit executable during local plan resolution."""
    _tmp_path, config, sockets = sandbox
    (config / "profiles.d" / "slurm-batch.toml").write_text(
        'version = 1\nname = "slurm-batch"\n'
        'job_id_pattern = "(?P<job_id>\\\\d+)"\n'
        'submit_command = ["bash", "{script}"]\n'
    )
    args = ["--json"] if json_flag else []
    proc = submit(fleetctl_path, sandbox, payload, *args)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "must invoke `sbatch` directly" in (proc.stdout + proc.stderr)
    assert not any(sockets.iterdir()), "invalid profile opened a connection"


# -- paths name the cluster, so they never refuse from here --------------------


@pytest.fixture()
def cluster_path_job(tmp_path):
    script = tmp_path / "job.sbatch"
    script.write_text(
        "#!/bin/bash\n"
        "#SBATCH --job-name=train\n"
        "#SBATCH --partition=a100\n"
        "#SBATCH --account=research\n"
        "#SBATCH --chdir=/storage/nobody/proj\n"
        "#SBATCH --time=04:00:00\n"
        "#SBATCH --output=/storage/nobody/logs/%j.out\n"
        "\n"
        "cd /storage/nobody/proj\n"
        "/storage/nobody/venv/bin/python train.py\n"
    )
    return script


def test_native_job_with_cluster_paths_is_submitted(fleetctl_path, sandbox, cluster_path_job):
    proc = submit(fleetctl_path, sandbox, cluster_path_job, "--native-batch")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for rule in ("FS001", "FS002", "FS003"):
        assert f"INFO  {rule}" in proc.stderr


def test_strict_does_not_turn_path_hints_into_refusals(
    fleetctl_path, sandbox, cluster_path_job
):
    """--strict promotes warnings; a fact about another machine is not one."""
    proc = submit(fleetctl_path, sandbox, cluster_path_job, "--native-batch", "--strict")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_native_job_is_still_refused_for_real_policy(fleetctl_path, sandbox, tmp_path):
    script = tmp_path / "denied.sbatch"
    script.write_text(
        "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --account=chiru\n"
        "#SBATCH --time=01:00:00\n\npython3 train.py\n"
    )
    proc = submit(fleetctl_path, sandbox, script, "--native-batch")
    assert proc.returncode == 2
    assert "KIAC023" in proc.stderr


def test_no_preflight_still_skips_the_gate(fleetctl_path, sandbox, tmp_path):
    script = tmp_path / "denied.sbatch"
    script.write_text("#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --account=chiru\n")
    proc = submit(fleetctl_path, sandbox, script, "--native-batch", "--no-preflight")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "KIAC023" not in proc.stderr


# -- --account/--qos overrides are checked as the *effective* values (item 3) -


def test_account_override_lets_h200_pass_its_own_required_qos(fleetctl_path, sandbox, payload):
    """h200 presets no account (account_required=true) but does preset its own
    qos ("h200_qos", matching required_qos); --account chiru alone should be
    enough for a queue whose allowed_accounts includes 'chiru'."""
    proc = submit(fleetctl_path, sandbox, payload, "--queue", "h200", "--account", "chiru")
    # returncode 0 alone already proves no site-policy ERROR fired: submit's
    # preflight gate raises whenever the report's worst level is ERROR.
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The wrapper (printed as the dry-run's rendered-script block, on stdout)
    # carries the effective account, proving the override reached #SBATCH.
    assert "#SBATCH --account=chiru" in proc.stdout


def test_account_override_with_a_denied_account_is_refused(fleetctl_path, sandbox, payload):
    """a100's denied_accounts includes 'chiru': the override is checked, not
    just the preset."""
    proc = submit(fleetctl_path, sandbox, payload, "--queue", "a100", "--account", "chiru")
    assert proc.returncode == 2
    assert "KIAC023" in proc.stderr


def test_qos_override_replaces_the_queues_own_qos_in_the_wrapper(fleetctl_path, sandbox, payload):
    proc = submit(
        fleetctl_path, sandbox, payload,
        "--queue", "h200", "--account", "chiru", "--qos", "other_qos",
    )
    # required_qos is "h200_qos"; overriding to "other_qos" must be caught.
    assert proc.returncode == 2
    assert "KIAC024" in proc.stderr
    assert "other_qos" in proc.stderr


def test_native_batch_refuses_account_and_qos_flags(fleetctl_path, sandbox, tmp_path):
    script = tmp_path / "job.sbatch"
    script.write_text(
        "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --account=research\n"
        "#SBATCH --time=01:00:00\n\npython3 train.py\n"
    )
    proc = submit(fleetctl_path, sandbox, script, "--native-batch", "--account", "research")
    assert proc.returncode == 2
    assert "--account" in proc.stderr
    assert "cannot be applied here" in proc.stderr

    proc = submit(fleetctl_path, sandbox, script, "--native-batch", "--qos", "normal")
    assert proc.returncode == 2
    assert "--qos" in proc.stderr
