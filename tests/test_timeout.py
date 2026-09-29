"""`--timeout` (item 2): one deadline over every local command a verb runs.

These run real local processes only (sh, sleep, setsid) -- nothing dials out.
"""

from __future__ import annotations

import os
import time

import pytest


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_deadline_expiry_kills_the_whole_process_group(fleetctl, tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    script = f"sleep 30 & echo $! > {pidfile}; wait"
    start = time.monotonic()
    with fleetctl.deadline_scope(0.5):
        with pytest.raises(fleetctl.FleetTimeout) as excinfo:
            fleetctl.run_command(["sh", "-c", script], capture_output=True)
    assert time.monotonic() - start < 5
    assert excinfo.value.exit_code == 124
    grandchild = int(pidfile.read_text())
    deadline = time.monotonic() + 2
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild), "a child the command spawned outlived --timeout"


def test_a_plain_per_call_timeout_stays_an_ordinary_fleet_error(fleetctl):
    with pytest.raises(fleetctl.FleetError) as excinfo:
        fleetctl.run_command(["sleep", "30"], capture_output=True, timeout=0.3)
    assert not isinstance(excinfo.value, fleetctl.FleetTimeout)


def test_the_tighter_of_deadline_and_call_timeout_wins(fleetctl):
    with fleetctl.deadline_scope(30):
        with pytest.raises(fleetctl.FleetError) as excinfo:
            fleetctl.run_command(["sleep", "30"], capture_output=True, timeout=0.3)
    assert not isinstance(excinfo.value, fleetctl.FleetTimeout)


def test_an_exhausted_deadline_refuses_before_spawning(fleetctl, tmp_path):
    marker = tmp_path / "ran"
    with fleetctl.deadline_scope(0.0):
        with pytest.raises(fleetctl.FleetTimeout) as excinfo:
            fleetctl.run_command(["touch", str(marker)])
    assert excinfo.value.sent is False
    assert not marker.exists()


@pytest.mark.parametrize("limited", [False, True])
def test_a_descendant_in_another_session_cannot_hang_the_drain(fleetctl, monkeypatch, limited):
    """ssh under sshpass runs in its own session and keeps our stdout pipe; after
    the kill, draining is bounded instead of waiting for it to exit."""
    monkeypatch.setattr(fleetctl, "POST_KILL_DRAIN_TIMEOUT", 0.5)
    kwargs = {"stdout_limit": 1024, "stderr_limit": 1024} if limited else {}
    start = time.monotonic()
    with fleetctl.deadline_scope(0.5):
        with pytest.raises(fleetctl.FleetTimeout):
            fleetctl.run_command(["sh", "-c", "setsid sleep 5 & wait"], capture_output=True, **kwargs)
    assert time.monotonic() - start < 3.5


def test_capture_limits_truncate_without_blocking_the_child(fleetctl):
    completed = fleetctl.run_command(
        ["sh", "-c", "head -c 300000 /dev/zero | tr '\\0' x"],
        capture_output=True, stdout_limit=1000, stderr_limit=1000,
    )
    assert completed.returncode == 0
    assert len(completed.stdout) == 1000
    assert completed.stdout_truncated is True
    assert completed.stderr_truncated is False
