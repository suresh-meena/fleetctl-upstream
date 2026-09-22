"""The `--live` stage: `sbatch --test-only` over fleetctl's own transport.

Nothing here reaches a host.  `run_ssh_command` and `stage_remote_script` are
replaced, so these tests pin the part that is ours -- which argv gets built,
how each outcome is classed, and that the staged copy is always cleaned up --
rather than re-testing ssh.
"""

import subprocess
import types

import pytest


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def plan(fleetctl):
    profile = fleetctl.ProfileRecord(
        name="slurm-batch", submit_command=("sbatch", "{script}")
    )
    return types.SimpleNamespace(
        profile=profile,
        secret=object(),
        cwd="/home/u/work",
        target=types.SimpleNamespace(name="kiac-login"),
        connect=lambda: {},
    )


@pytest.fixture
def calls(fleetctl, monkeypatch):
    """Record every remote command, and let each test choose the sbatch result."""
    recorded = {"argv": [], "result": _completed(0, "Job 1 to start at ...")}

    monkeypatch.setattr(
        fleetctl, "stage_remote_script", lambda *a, **k: "/scratch/tok/job.sbatch"
    )

    def fake_run(secret, *, remote_command, **kwargs):
        recorded["argv"].append(remote_command)
        if "rm -f" in remote_command:
            return _completed(0)
        return recorded["result"]

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake_run)
    return recorded


def test_accepted_script_records_live001_and_the_caveat(fleetctl, plan, calls, tmp_path):
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\n#SBATCH --partition=short\n")
    rep = fleetctl.Report()

    fleetctl.live_test_only(plan, script, rep)

    ids = rep.rule_ids()
    assert "LIVE001" in ids and "LIVE002" in ids
    live001 = [d for d in rep.items if d.rule_id == "LIVE001"][0]
    assert live001.level == "PASS"
    assert live001.confidence == "verified-live"
    # The caveat is the whole point: a green dry run is not account/QOS proof.
    caveat = [d for d in rep.items if d.rule_id == "LIVE002"][0]
    assert caveat.level == "INFO"
    assert "real five-minute job" in caveat.message


def test_test_only_flag_precedes_the_script(fleetctl, plan, calls, tmp_path):
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\n")
    fleetctl.live_test_only(plan, script, fleetctl.Report())

    sbatch = [c for c in calls["argv"] if "sbatch" in c][0]
    assert "--test-only" in sbatch
    assert sbatch.index("--test-only") < sbatch.index("/scratch/tok/job.sbatch")


def test_rejected_script_is_an_error_carrying_the_scheduler_reason(
    fleetctl, plan, calls, tmp_path
):
    calls["result"] = _completed(1, "", "sbatch: error: Invalid partition name")
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\n")
    rep = fleetctl.Report()

    fleetctl.live_test_only(plan, script, rep)

    live003 = [d for d in rep.items if d.rule_id == "LIVE003"][0]
    assert live003.level == "ERROR"
    assert "Invalid partition name" in live003.excerpt
    assert "LIVE002" not in rep.rule_ids()


def test_staged_copy_is_removed_even_when_sbatch_fails(
    fleetctl, plan, calls, tmp_path
):
    calls["result"] = _completed(1, "", "boom")
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\n")

    fleetctl.live_test_only(plan, script, fleetctl.Report())

    assert any("rm -f" in c and "/scratch/tok/job.sbatch" in c for c in calls["argv"])


def test_staged_copy_is_removed_when_the_connection_raises(
    fleetctl, plan, calls, tmp_path, monkeypatch
):
    def blow_up(secret, *, remote_command, **kwargs):
        calls["argv"].append(remote_command)
        if "rm -f" in remote_command:
            return _completed(0)
        raise fleetctl.FleetError("connection died mid-command")

    monkeypatch.setattr(fleetctl, "run_ssh_command", blow_up)
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\n")

    with pytest.raises(fleetctl.FleetError):
        fleetctl.live_test_only(plan, script, fleetctl.Report())
    assert any("rm -f" in c for c in calls["argv"])


def test_refuses_a_submit_command_it_cannot_dry_run(fleetctl, plan, calls, tmp_path):
    """Splicing --test-only into an unknown wrapper would submit for real."""
    plan.profile = fleetctl.ProfileRecord(
        name="apptainer-batch",
        submit_command=("apptainer", "exec", "img.sif", "sbatch", "{script}"),
    )
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\n")

    with pytest.raises(fleetctl.FleetError) as exc:
        fleetctl.live_test_only(plan, script, fleetctl.Report())
    assert "knows how to dry-run" in str(exc.value)
    assert calls["argv"] == []


def test_refuses_a_profile_that_does_not_submit(fleetctl, plan, calls, tmp_path):
    plan.profile = fleetctl.ProfileRecord(name="plain-ssh")
    script = tmp_path / "job.sbatch"
    script.write_text("#!/bin/bash\n")

    with pytest.raises(fleetctl.FleetError) as exc:
        fleetctl.live_test_only(plan, script, fleetctl.Report())
    assert "drop --live" in str(exc.value)
