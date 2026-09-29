"""Admission policy for scheduler login surfaces."""

import pytest


def test_login_script_is_refused_even_with_admin(fleetctl):
    target = fleetctl.TargetRecord(name="login", role="login")
    protocol = fleetctl.ProtocolRecord(name="slurm", kind="slurm")

    with pytest.raises(fleetctl.FleetError, match="ssh.*--admin"):
        fleetctl.authorize("ssh", target, protocol)
    assert fleetctl.authorize("ssh", target, protocol, admin=True) == "login"
    assert fleetctl.authorize("exec", target, protocol, admin=True) == "login"
    with pytest.raises(fleetctl.FleetError, match="script.*refused"):
        fleetctl.authorize("script", target, protocol, admin=True)


def test_slurm_submit_profile_accepts_absolute_sbatch_path(fleetctl):
    target = fleetctl.TargetRecord(name="login", role="login")
    protocol = fleetctl.ProtocolRecord(name="slurm", kind="slurm")
    profile = fleetctl.ProfileRecord(
        name="site-slurm",
        submit_command=("/opt/slurm/bin/sbatch", "--account=research", "{script}"),
    )

    fleetctl.check_protocol_profile(target, protocol, profile)


@pytest.mark.parametrize(
    "command",
    [
        ("./sbatch", "{script}"),
        ("bin/sbatch", "{script}"),
        ("sbatch-wrapper", "{script}"),
        ("sbatch", "{script}", "--account=research"),
        ("sbatch", "--wrap", "echo unsafe", "{script}"),
        ("sbatch", "--wrap=echo unsafe", "{script}"),
    ],
)
def test_slurm_submit_profile_requires_safe_script_argv(fleetctl, command):
    target = fleetctl.TargetRecord(name="login", role="login")
    protocol = fleetctl.ProtocolRecord(name="slurm", kind="slurm")
    profile = fleetctl.ProfileRecord(name="site-slurm", submit_command=command)

    with pytest.raises(fleetctl.FleetError):
        fleetctl.check_protocol_profile(target, protocol, profile)
