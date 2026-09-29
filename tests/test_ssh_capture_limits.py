"""SSH capture-limit forwarding without contacting a remote host."""

from __future__ import annotations

import subprocess


def test_run_ssh_command_forwards_capture_limits_to_run_command(fleetctl, monkeypatch):
    seen = {}

    def fake_run_command(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(fleetctl, "run_command", fake_run_command)
    secret = fleetctl.FleetSecret(host="192.0.2.1", user="test")

    result = fleetctl.run_ssh_command(
        secret,
        allocate_tty=False,
        remote_command="true",
        capture_output=True,
        stdout_limit=123,
        stderr_limit=456,
    )

    assert result.returncode == 0
    assert seen["kwargs"]["capture_output"] is True
    assert seen["kwargs"]["stdout_limit"] == 123
    assert seen["kwargs"]["stderr_limit"] == 456
