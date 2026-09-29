"""Peer transfer failures must not repeat a dispatched rsync through relay."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_peer_rsync_failure_is_marked_uncertain(fleetctl, monkeypatch):
    source = SimpleNamespace(
        secret=fleetctl.FleetSecret(host="source", user="u"),
        target=SimpleNamespace(name="source"),
        connect=lambda: {},
    )
    dest = SimpleNamespace(
        secret=fleetctl.FleetSecret(host="dest", user="u"),
        target=fleetctl.TargetRecord(
            name="dest", role="workstation", protocol="direct"
        ),
    )
    calls = []

    def fake_ssh(secret, *, remote_command, **kwargs):
        calls.append(remote_command)
        code = 23 if len(calls) == 3 else 0
        output = "/dst/project\n/home/u\n" if len(calls) == 1 else ""
        return SimpleNamespace(returncode=code, stdout=output, stderr="rsync failed")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake_ssh)

    with pytest.raises(fleetctl.PeerTransferUncertain, match="may be partially changed"):
        fleetctl.run_peer_transfer(
            source, dest, "/src", "/dst/project", level=0, delete=True,
            force=False, excludes=()
        )

    assert len(calls) == 3  # resolve, probe, then peer rsync


def test_peer_delete_refuses_destination_resolving_to_home_before_mkdir(
    fleetctl, monkeypatch
):
    source = SimpleNamespace(
        secret=fleetctl.FleetSecret(host="source", user="u"),
        target=SimpleNamespace(name="source"),
        connect=lambda: {},
    )
    dest = SimpleNamespace(
        secret=fleetctl.FleetSecret(host="dest", user="u"),
        target=fleetctl.TargetRecord(name="dest", role="workstation", protocol="direct"),
    )
    calls = []

    def fake_ssh(secret, *, remote_command, **kwargs):
        calls.append(remote_command)
        return SimpleNamespace(returncode=0, stdout="/home/u\n/home/u\n", stderr="")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake_ssh)
    with pytest.raises(fleetctl.FleetError, match="remote home"):
        fleetctl.run_peer_transfer(
            source, dest, "/src", "/safe/link", level=0,
            delete=True, force=True, excludes=()
        )
    assert len(calls) == 1


def test_uncertain_peer_failure_stops_without_relay(fleetctl, monkeypatch, capsys):
    source = SimpleNamespace(target=SimpleNamespace(name="source"), binding=None)
    dest = SimpleNamespace(target=SimpleNamespace(name="dest"), binding=None)
    monkeypatch.setattr(fleetctl, "transfer_endpoint_plan", lambda *_args: source)
    monkeypatch.setattr(fleetctl, "build_plan", lambda *_args: dest)
    monkeypatch.setattr(fleetctl, "resolve_transfer_path", lambda *_args, **_kw: "/path")
    monkeypatch.setattr(fleetctl, "central_sync_budget_bytes", lambda *_args: 1)
    monkeypatch.setattr(fleetctl, "enforce_control_budget", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        fleetctl,
        "run_peer_transfer",
        lambda *_args, **_kw: (_ for _ in ()).throw(
            fleetctl.PeerTransferUncertain("partial transfer", exit_code=23)
        ),
    )

    def relay_must_not_run(*_args, **_kwargs):
        pytest.fail("relay would repeat a potentially partial peer transfer")

    monkeypatch.setattr(fleetctl, "run_relay_transfer", relay_must_not_run)
    args = SimpleNamespace(
        selector=None,
        local_path="source",
        remote_path="dest",
        delete=False,
        force=False,
        dry_run=False,
        relay=False,
        no_fallback=False,
    )

    with pytest.raises(fleetctl.PeerTransferUncertain, match="partial transfer"):
        fleetctl.command_sync_transfer(args, object(), level=0)

    assert "routing through this machine" not in capsys.readouterr().err


def test_pre_dispatch_peer_error_still_uses_relay(fleetctl, monkeypatch, capsys):
    source = SimpleNamespace(target=SimpleNamespace(name="source"), binding=None)
    dest = SimpleNamespace(target=SimpleNamespace(name="dest"), binding=None)
    monkeypatch.setattr(fleetctl, "transfer_endpoint_plan", lambda *_args: source)
    monkeypatch.setattr(fleetctl, "build_plan", lambda *_args: dest)
    monkeypatch.setattr(fleetctl, "resolve_transfer_path", lambda *_args, **_kw: "/path")
    monkeypatch.setattr(fleetctl, "central_sync_budget_bytes", lambda *_args: 1)
    monkeypatch.setattr(fleetctl, "enforce_control_budget", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        fleetctl,
        "run_peer_transfer",
        lambda *_args, **_kw: (_ for _ in ()).throw(fleetctl.FleetError("probe failed")),
    )
    relayed = []
    monkeypatch.setattr(
        fleetctl, "run_relay_transfer", lambda *_args, **_kw: relayed.append(True) or 0
    )
    args = SimpleNamespace(
        selector=None,
        local_path="source",
        remote_path="dest",
        delete=False,
        force=False,
        dry_run=False,
        relay=False,
        no_fallback=False,
    )

    assert fleetctl.command_sync_transfer(args, object(), level=0) == 0
    assert relayed == [True]
    assert "probe failed" in capsys.readouterr().err


def test_json_uncertain_peer_failure_stops_without_relay(fleetctl, monkeypatch):
    """`--json` follows the text path's rule instead of relaying over a partial copy."""
    source = SimpleNamespace(target=SimpleNamespace(name="source"), binding=None)
    dest = SimpleNamespace(target=SimpleNamespace(name="dest"), binding=None)
    monkeypatch.setattr(fleetctl, "transfer_endpoint_plan", lambda *_args: source)
    monkeypatch.setattr(fleetctl, "build_plan", lambda *_args: dest)
    monkeypatch.setattr(fleetctl, "resolve_transfer_path", lambda *_args, **_kw: "/path")
    monkeypatch.setattr(fleetctl, "central_sync_budget_bytes", lambda *_args: 1)
    monkeypatch.setattr(fleetctl, "enforce_control_budget", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        fleetctl,
        "run_peer_transfer",
        lambda *_args, **_kw: (_ for _ in ()).throw(
            fleetctl.PeerTransferUncertain("partial transfer", exit_code=23)
        ),
    )

    def relay_must_not_run(*_args, **_kwargs):
        pytest.fail("relay would repeat a potentially partial peer transfer")

    monkeypatch.setattr(fleetctl, "run_relay_transfer", relay_must_not_run)
    args = SimpleNamespace(
        expected_role=None, selector=None, local_path="source", remote_path="dest",
        delete=False, force=False, dry_run=False, relay=False, no_fallback=False,
    )
    with pytest.raises(fleetctl.PeerTransferUncertain, match="partial transfer"):
        fleetctl._json_sync_transfer(
            args, object(), level=0, start=0.0, attempt=fleetctl.Attempt()
        )
