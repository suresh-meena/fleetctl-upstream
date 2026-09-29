import subprocess
import types
import pytest


@pytest.mark.parametrize("root", [
    "/home/alice", "/home/alice/shared", "/tmp/fleet/scripts",
    "/etc/fleet/scripts", "/usr/local/fleet/scripts",
    "/home/alice/../shared/fleet/scripts", "$HOME/.local/state/fleet/scripts",
    "~/shared/fleet/scripts", "relative/fleet/scripts",
])
def test_stage_root_rejects_broad_mistyped_or_aliased_paths(fleetctl, root):
    with pytest.raises(fleetctl.FleetError):
        fleetctl.validate_remote_script_root(root, root)


@pytest.mark.parametrize(("configured", "resolved", "expected"), [
    (".local/state/fleet/scripts", "/home/alice/.local/state/fleet/scripts", "/home/alice/.local/state/fleet/scripts"),
    ("/srv/fleet/scripts", "/srv/fleet/scripts", "/srv/fleet/scripts"),
])
def test_stage_root_accepts_dedicated_roots(fleetctl, configured, resolved, expected):
    assert fleetctl.validate_remote_script_root(configured, resolved) == expected


def test_prune_command_refuses_unvalidated_root_and_directory(fleetctl):
    with pytest.raises(fleetctl.FleetError):
        fleetctl.stage_prune_command("/home/alice", "/home/alice/" + "a" * 16)
    with pytest.raises(fleetctl.FleetError):
        fleetctl.stage_prune_command(
            "/srv/fleet/scripts", "/srv/fleet/scripts/shared"
        )


def test_invalid_stage_root_fails_before_remote_mutation_or_upload(
    fleetctl, monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(fleetctl, "resolve_remote_home", lambda *a, **k: "/home/alice")
    monkeypatch.setattr(
        fleetctl, "run_ssh_command",
        lambda *a, **k: calls.append(("ssh", k.get("remote_command")))
        or subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(fleetctl, "run_command", lambda *a, **k: calls.append(("scp", None)))
    profile = fleetctl.ProfileRecord(name="bad", remote_script_root="/home/alice/shared")

    with pytest.raises(fleetctl.FleetError):
        fleetctl.stage_remote_script(object(), profile, tmp_path / "job.sh")
    assert calls == []


def test_default_root_stages_only_under_resolved_dedicated_directory(
    fleetctl, monkeypatch, tmp_path
):
    calls = []
    monkeypatch.setattr(fleetctl.secrets, "token_hex", lambda _: "a" * 16)
    monkeypatch.setattr(fleetctl, "resolve_remote_home", lambda *a, **k: "/home/alice")
    monkeypatch.setattr(
        fleetctl, "run_ssh_command",
        lambda *a, **k: calls.append(k["remote_command"])
        or subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(fleetctl, "build_scp_command", lambda *a, **k: ["scp"])
    monkeypatch.setattr(fleetctl, "ssh_env", lambda *a, **k: {})
    monkeypatch.setattr(fleetctl, "run_command", lambda *a, **k: subprocess.CompletedProcess([], 0, "", ""))
    profile = fleetctl.ProfileRecord(name="ok")
    result = fleetctl.stage_remote_script(types.SimpleNamespace(target="alice@host"), profile, tmp_path / "job.sh")
    assert result == "/home/alice/.local/state/fleet/scripts/" + "a" * 16 + "/job.sh"
    assert "/home/alice/.local/state/fleet/scripts" in calls[0]
