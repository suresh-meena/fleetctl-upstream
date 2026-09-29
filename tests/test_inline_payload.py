"""Inline small payloads into the Slurm wrapper (item 4).

A payload script <= 64 KiB is embedded in the rendered wrapper (base64 inside
a quoted heredoc) instead of being staged separately, so the queue path stages
only the wrapper.  Pure unit tests against render_slurm_wrapper/
payload_fits_inline directly, plus one CLI-level test (dry-run, sandbox
pattern from test_submit_gate.py) proving command_submit's queue path never
stages the payload when it fits.

Nothing here touches a network: the only subprocess is a local `bash -n`.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from _helpers import make_protocol, make_queue


def render_with_payload(fleetctl, payload: bytes, *, script_args=()) -> str:
    protocol = make_protocol(fleetctl)
    queue = make_queue(fleetctl, name="q", partition="q")
    return fleetctl.render_slurm_wrapper(
        protocol=protocol,
        remote_script="/never/used",
        script_args=list(script_args),
        interpreter="bash",
        environment={},
        cwd=None,
        queue=queue,
        job_name="job",
        time_limit=None,
        gpus=None,
        cpus_per_task=None,
        mem=None,
        output_path=None,
        error_path=None,
        inline_payload=payload,
    )


def extract_delimiter(text: str) -> str:
    lines = text.splitlines()
    opener = next(line for line in lines if line.startswith("base64 -d"))
    return opener.split("<<'")[1].rstrip("'")


def decode_body(text: str, delimiter: str) -> bytes:
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("base64 -d"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == delimiter)
    body = "".join(lines[start + 1 : end])
    return base64.b64decode(body)


PAYLOADS = [
    b"#!/bin/bash\nprint 'hi'\n",
    b"echo \"double $VAR quotes\"\n",
    b"echo 'single quotes'\n",
    b"echo `backticks`\n",
    b"echo $(command substitution)\n",
    b"line one\nEOF\nline three\n",  # a line that is exactly EOF
    bytes(range(256)),  # binary-ish
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_delimiter_appears_exactly_twice(fleetctl, payload):
    text = render_with_payload(fleetctl, payload)
    delimiter = extract_delimiter(text)
    assert text.count(delimiter) == 2


@pytest.mark.parametrize("payload", PAYLOADS)
def test_round_trip_reproduces_payload_byte_for_byte(fleetctl, payload):
    text = render_with_payload(fleetctl, payload)
    delimiter = extract_delimiter(text)
    assert decode_body(text, delimiter) == payload


@pytest.mark.parametrize("payload", PAYLOADS)
def test_rendered_wrapper_passes_bash_dash_n(fleetctl, payload):
    text = render_with_payload(fleetctl, payload)
    result = subprocess.run(
        ["bash", "-n"], input=text, text=True, capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr


def test_delimiter_shape_is_32_hex_between_fixed_prefix_and_suffix(fleetctl):
    text = render_with_payload(fleetctl, b"x\n")
    delimiter = extract_delimiter(text)
    assert delimiter.startswith("FLEETCTL_PAYLOAD_")
    assert delimiter.endswith("_END")
    hexpart = delimiter[len("FLEETCTL_PAYLOAD_") : -len("_END")]
    assert len(hexpart) == 32
    assert all(ch in "0123456789abcdef" for ch in hexpart)


def test_exit_code_of_the_payload_is_preserved(fleetctl):
    text = render_with_payload(fleetctl, b"#!/bin/sh\nexit 7\n")
    # Strip the #SBATCH/env preamble: run just the generated tail in a fresh
    # shell to prove the wrapper's own exit code equals the payload's.
    lines = text.splitlines()
    tail_start = next(i for i, line in enumerate(lines) if line == "umask 077")
    tail = "\n".join(lines[tail_start:])
    result = subprocess.run(["bash", "-c", tail], text=True, capture_output=True, timeout=10)
    assert result.returncode == 7


def test_scancel_sigterm_still_removes_the_payload_and_exits_143(fleetctl, tmp_path):
    """dash skips EXIT traps on an untrapped signal; the TERM trap turns scancel's
    SIGTERM into an ordinary exit, so the private temp file never outlives the job."""
    import os
    import signal
    import time

    text = render_with_payload(fleetctl, b"#!/bin/sh\nsleep 30\n")
    lines = text.splitlines()
    tail = "\n".join(lines[lines.index("umask 077"):])
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    shell = subprocess.Popen(["sh", "-c", tail], env=env, start_new_session=True)
    deadline = time.monotonic() + 5
    while not list(tmp_path.glob("fleetctl-payload.*")) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert list(tmp_path.glob("fleetctl-payload.*")), "payload was never written"
    time.sleep(0.2)
    os.killpg(shell.pid, signal.SIGTERM)  # Slurm signals every process in the step
    assert shell.wait(timeout=5) == 143
    assert not list(tmp_path.glob("fleetctl-payload.*"))


def test_script_args_are_individually_quoted_and_the_payload_var_still_expands(fleetctl):
    text = render_with_payload(fleetctl, b"#!/bin/sh\nprintf '%s\\n' \"$@\"\n", script_args=["a b", "c"])
    lines = text.splitlines()
    run_line = lines[[i for i, l in enumerate(lines) if l == extract_delimiter(text)][-1] + 1]
    assert '"$__fleetctl_payload"' in run_line
    assert "'a b'" in run_line
    result = subprocess.run(["bash", "-c", "\n".join(lines[lines.index("umask 077"):])], text=True, capture_output=True, timeout=10)
    assert result.stdout.splitlines() == ["a b", "c"]


# -- payload size threshold ---------------------------------------------------


def test_payload_fits_inline_true_at_and_under_the_cap(fleetctl, tmp_path):
    small = tmp_path / "small.sh"
    small.write_bytes(b"x" * fleetctl.INLINE_PAYLOAD_MAX_BYTES)
    assert fleetctl.payload_fits_inline(small) is True


def test_payload_fits_inline_false_over_the_cap(fleetctl, tmp_path):
    big = tmp_path / "big.sh"
    big.write_bytes(b"x" * (fleetctl.INLINE_PAYLOAD_MAX_BYTES + 1))
    assert fleetctl.payload_fits_inline(big) is False


def test_over_cap_payload_keeps_the_old_path_referencing_tail(fleetctl, tmp_path):
    """>64 KiB: render_slurm_wrapper's tail is unchanged (no inline_payload given)."""
    protocol = make_protocol(fleetctl)
    queue = make_queue(fleetctl, name="q", partition="q")
    text = fleetctl.render_slurm_wrapper(
        protocol=protocol,
        remote_script="/scratch/tok/job.sh",
        script_args=["a"],
        interpreter="bash",
        environment={},
        cwd=None,
        queue=queue,
        job_name="job",
        time_limit=None,
        gpus=None,
        cpus_per_task=None,
        mem=None,
        output_path=None,
        error_path=None,
        inline_payload=None,
    )
    assert "FLEETCTL_PAYLOAD_" not in text
    assert "bash /scratch/tok/job.sh a" in text


# -- CLI-level: the queue path stages only the wrapper for a small payload ---

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
account = "research"
time_limit = "00:10:00"
max_time = "24:00:00"
evidence = "verified-live"
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


def test_dry_run_reports_the_payload_as_inlined_not_staged(fleetctl_path, sandbox, tmp_path):
    tmp_path_, config, sockets = sandbox
    script = tmp_path / "train.sh"
    script.write_text("#!/bin/bash\nset -euo pipefail\npython3 train.py\n")
    proc = subprocess.run(
        [
            sys.executable, str(fleetctl_path),
            "--config-home", str(config),
            "--state-home", str(tmp_path_ / "state"),
            "--cache-home", str(tmp_path_ / "cache"),
            "submit", str(script), "--target", "login", "--queue", "short", "--dry-run",
        ],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "(inlined into wrapper)" in proc.stdout
    assert "FLEETCTL_PAYLOAD_" in proc.stdout
    assert not any(sockets.iterdir()), "a dry run opened a connection"
