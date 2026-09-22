"""End-to-end `fleetctl preflight` invocations through a real subprocess.

Everything above this file exercises the preflight engine as a library.
These few tests instead drive `bin/fleetctl preflight ... --target ...` the
way an operator actually would, to prove the CLI wiring itself (argument
parsing, protocol resolution from a TOML file, exit-code propagation) works
end to end -- while never touching the developer's real fleet config: every
invocation passes an explicit --config-home/--state-home/--cache-home
pointed at a tmp_path fleet config this test writes itself, so nothing here
reads or writes ~/.config/fleet or ~/.local/state/fleet.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

PROTOCOL_TOML = """\
kind = "slurm"
rule_prefix = "KIAC"
preferred_storage = "/storage"

[[queue]]
name = "medium"
partition = "medium"
max_time = "24:00:00"
evidence = "documented"

[[queue]]
name = "h200"
partition = "h200"
max_time = "24:00:00"
evidence = "verified-by-run"
as_of = "2026-09-14"
account_required = true
allowed_accounts = ["chiru"]
required_qos = "h200_qos"
"""


@pytest.fixture()
def fleet_config_home(tmp_path):
    """A minimal, throwaway fleet config: just enough for `preflight` to
    resolve a protocol by name, with no targets/pools/secrets at all."""
    config_home = tmp_path / "fleet-config"
    protocols_dir = config_home / "protocols.d"
    protocols_dir.mkdir(parents=True)
    (protocols_dir / "kiac.toml").write_text(PROTOCOL_TOML)
    return config_home


def run_fleetctl(fleetctl_path, tmp_path, config_home, *args):
    cmd = [
        sys.executable,
        str(fleetctl_path),
        "--config-home",
        str(config_home),
        "--state-home",
        str(tmp_path / "fleet-state"),
        "--cache-home",
        str(tmp_path / "fleet-cache"),
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def test_preflight_clean_script_exits_zero(fleetctl_path, fleet_config_home, tmp_path):
    script = tmp_path / "clean.sbatch"
    script.write_text(
        "#!/bin/bash\n"
        "#SBATCH --job-name=clean\n"
        "#SBATCH --partition=medium\n"
        "#SBATCH --time=01:00:00\n"
        "#SBATCH --mem=4G\n"
        "#SBATCH --output=clean.out\n"
        "#SBATCH --error=clean.err\n"
        "echo hello\n"
    )
    proc = run_fleetctl(
        fleetctl_path, tmp_path, fleet_config_home, "preflight", str(script), "--target", "kiac",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    summary = proc.stdout.strip().splitlines()[-1]
    assert "0 WARN, 0 ERROR" in summary
    assert "clean.sbatch" in summary


def test_preflight_warnings_only_exits_one(fleetctl_path, fleet_config_home, tmp_path):
    script = tmp_path / "warn.sbatch"
    script.write_text(
        "#!/bin/bash\n"
        "#SBATCH --job-name=warn\n"
        "#SBATCH --partition=medium\n"
        "#SBATCH --time=01:00:00\n"
        "#SBATCH --mem=4G\n"
        # stdout and stderr naming one file is a WARN (SLURM042) on any host,
        # which is what this fixture needs: a warning that is not an error.
        "#SBATCH --output=job.out\n"
        "#SBATCH --error=job.out\n"
        "echo hello\n"
    )
    proc = run_fleetctl(
        fleetctl_path, tmp_path, fleet_config_home, "preflight", str(script), "--target", "kiac",
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "SLURM042" in proc.stdout


def test_preflight_error_exits_two(fleetctl_path, fleet_config_home, tmp_path):
    script = tmp_path / "err.sbatch"
    script.write_text(
        "#!/bin/bash\n"
        "#SBATCH --job-name=err\n"
        "#SBATCH --partition=doesnotexist\n"
        "#SBATCH --time=01:00:00\n"
        "#SBATCH --mem=4G\n"
        "#SBATCH --output=err.out\n"
        "echo hello\n"
    )
    proc = run_fleetctl(
        fleetctl_path, tmp_path, fleet_config_home, "preflight", str(script), "--target", "kiac",
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "KIAC011" in proc.stdout


def test_preflight_json_flag_emits_parseable_payload(fleetctl_path, fleet_config_home, tmp_path):
    script = tmp_path / "err.sbatch"
    script.write_text(
        "#!/bin/bash\n"
        "#SBATCH --partition=doesnotexist\n"
        "#SBATCH --time=01:00:00\n"
        "#SBATCH --mem=4G\n"
        "echo hello\n"
    )
    proc = run_fleetctl(
        fleetctl_path, tmp_path, fleet_config_home,
        "preflight", str(script), "--target", "kiac", "--json",
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["exit_code"] == 2
    assert payload["protocol"] == "kiac"
    rule_ids = {d["rule_id"] for d in payload["diagnostics"]}
    assert "KIAC011" in rule_ids


def test_preflight_strict_escalates_to_two(fleetctl_path, fleet_config_home, tmp_path):
    script = tmp_path / "warn.sbatch"
    script.write_text(
        "#!/bin/bash\n"
        "#SBATCH --partition=medium\n"
        "#SBATCH --time=01:00:00\n"
        "#SBATCH --mem=4G\n"
        "#SBATCH --output=job.out\n"
        "#SBATCH --error=job.out\n"
        "echo hello\n"
    )
    plain = run_fleetctl(
        fleetctl_path, tmp_path, fleet_config_home, "preflight", str(script), "--target", "kiac",
    )
    assert plain.returncode == 1

    strict = run_fleetctl(
        fleetctl_path, tmp_path, fleet_config_home,
        "preflight", str(script), "--target", "kiac", "--strict",
    )
    assert strict.returncode == 2, strict.stdout + strict.stderr


def test_preflight_missing_script_is_a_clean_refusal(fleetctl_path, fleet_config_home, tmp_path):
    missing = tmp_path / "nope.sbatch"
    proc = run_fleetctl(
        fleetctl_path, tmp_path, fleet_config_home, "preflight", str(missing), "--target", "kiac",
    )
    assert proc.returncode != 0
    assert "error" in proc.stderr.lower()


def test_preflight_never_touches_real_config_home(fleetctl_path, fleet_config_home, tmp_path, monkeypatch):
    """Belt-and-braces: even if $HOME resolved to something real in this
    process, FLEET_CONFIG_HOME/STATE_HOME/CACHE_HOME must never be read
    because --config-home/--state-home/--cache-home were passed explicitly."""
    monkeypatch.setenv("FLEET_CONFIG_HOME", "/nonexistent/should-not-be-read")
    monkeypatch.setenv("FLEET_STATE_HOME", "/nonexistent/should-not-be-read")
    monkeypatch.setenv("FLEET_CACHE_HOME", "/nonexistent/should-not-be-read")
    script = tmp_path / "clean.sbatch"
    script.write_text(
        "#!/bin/bash\n#SBATCH --partition=medium\n#SBATCH --time=01:00:00\n"
        "#SBATCH --mem=4G\necho hi\n"
    )
    proc = run_fleetctl(
        fleetctl_path, tmp_path, fleet_config_home, "preflight", str(script), "--target", "kiac",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
