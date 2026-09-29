"""`[defaults] local_target` (item 7): a reach entry naming this host folds to
`direct`, and a remote verb aimed at this host itself is refused.

Mix of pure unit tests (_normalize_route_for_local, resolve_routes,
CONFIG_DEFAULTS_KEYS) and CLI-level tests through the real config-loading/
build_plan path, following test_submit_gate.py's unroutable-target sandbox
pattern (dry-run only, no connection ever opened).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


# -- pure unit: route folding --------------------------------------------------


def test_via_local_target_folds_to_direct(fleetctl):
    assert fleetctl._normalize_route_for_local("via:pi", "pi") == "direct"


def test_via_other_bridge_is_unaffected(fleetctl):
    assert fleetctl._normalize_route_for_local("via:pi", "other") == "via:pi"


def test_direct_is_unaffected(fleetctl):
    assert fleetctl._normalize_route_for_local("direct", "pi") == "direct"


def test_no_local_target_configured_changes_nothing(fleetctl):
    assert fleetctl._normalize_route_for_local("via:pi", None) == "via:pi"


def test_resolve_routes_folds_a_declared_bridge_hop(fleetctl):
    target = fleetctl.TargetRecord(name="gpu1", reach=("via:pi", "direct"))
    routes = fleetctl.resolve_routes(target, None, local_target="pi")
    # One direct attempt, not the same one twice.
    assert routes == ("direct",)


def test_resolve_routes_leaves_other_bridges_alone(fleetctl):
    target = fleetctl.TargetRecord(name="gpu1", reach=("via:pi", "direct"))
    routes = fleetctl.resolve_routes(target, None, local_target="elsewhere")
    assert routes == ("via:pi", "direct")


def test_explicit_route_flag_naming_the_local_bridge_still_resolves(fleetctl):
    target = fleetctl.TargetRecord(name="gpu1", reach=("via:pi", "direct"))
    routes = fleetctl.resolve_routes(target, "via:pi", local_target="pi")
    assert routes == ("direct",)


# -- config schema: local_target is now a known [defaults] key ---------------


def test_local_target_is_a_known_defaults_key(fleetctl):
    assert "local_target" in fleetctl.CONFIG_DEFAULTS_KEYS


def test_unrelated_bogus_defaults_key_is_still_rejected(fleetctl, tmp_path):
    with pytest.raises(fleetctl.FleetError, match="unrecognised"):
        fleetctl.validate_config_file(
            {"defaults": {"not_a_real_key": 1}}, source=tmp_path / "config.toml"
        )


def test_local_target_alone_validates_cleanly(fleetctl, tmp_path):
    fleetctl.validate_config_file(
        {"defaults": {"local_target": "pi"}}, source=tmp_path / "config.toml"
    )


# -- CLI sandbox: refusal + doctor warning ------------------------------------

PROTOCOL_TOML = 'version = 1\nname = "direct"\nkind = "direct"\n'


@pytest.fixture()
def sandbox(tmp_path):
    sockets = Path(tempfile.mkdtemp(prefix="fc-", dir="/tmp"))
    config = tmp_path / "cfg"
    for sub in ("targets.d", "protocols.d", "profiles.d", "secrets"):
        (config / sub).mkdir(parents=True)
    (config / "projects.toml").write_text("version = 1\n")
    secret = config / "secrets" / "login.toml"
    secret.write_text('host = "192.0.2.1"\nuser = "nobody"\nport = 9\n')
    secret.chmod(0o600)
    (config / "targets.d" / "here.toml").write_text(
        'version = 1\nname = "here"\nrole = "workstation"\nprotocol = "direct"\n'
        f'secret_backend = "file"\nsecret_ref = "{secret}"\n'
    )
    yield tmp_path, config, sockets
    shutil.rmtree(sockets, ignore_errors=True)


def run_fleetctl(fleetctl_path, sandbox, *args):
    tmp_path, config, sockets = sandbox
    proc = subprocess.run(
        [
            sys.executable, str(fleetctl_path),
            "--config-home", str(config),
            "--state-home", str(tmp_path / "state"),
            "--cache-home", str(tmp_path / "cache"),
            *args,
        ],
        capture_output=True, text=True, timeout=30,
    )
    return proc


def test_exec_against_the_local_target_itself_is_refused(fleetctl_path, sandbox):
    tmp_path, config, sockets = sandbox
    (config / "config.toml").write_text(
        f'version = 1\n\n[ssh]\ncontrol_path = "{sockets}/%C"\n\n'
        '[defaults]\nlocal_target = "here"\n'
    )
    proc = run_fleetctl(fleetctl_path, sandbox, "exec", "here", "--", "true")
    assert proc.returncode == 2
    assert "local_target" in proc.stderr
    assert "does not SSH to itself" in proc.stderr
    assert not any(sockets.iterdir()), "a refused command opened a connection"


def test_exec_against_a_different_target_is_unaffected_by_local_target(
    fleetctl_path, sandbox
):
    tmp_path, config, sockets = sandbox
    (config / "config.toml").write_text(
        f'version = 1\n\n[ssh]\ncontrol_path = "{sockets}/%C"\n\n'
        '[defaults]\nlocal_target = "somewhere-else"\n'
    )
    proc = run_fleetctl(fleetctl_path, sandbox, "exec", "here", "--dry-run", "--", "true")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_doctor_warns_when_a_bridge_name_matches_hostname_and_local_target_unset(
    fleetctl, tmp_path, monkeypatch
):
    config = tmp_path / "cfg"
    for sub in ("targets.d", "protocols.d", "profiles.d", "secrets"):
        (config / sub).mkdir(parents=True)
    (config / "config.toml").write_text("version = 1\n")
    (config / "projects.toml").write_text("version = 1\n")
    secret_bridge = config / "secrets" / "bridge.toml"
    secret_bridge.write_text('host = "192.0.2.2"\nuser = "nobody"\nport = 9\nauth = "key"\nidentity_file = "~/.ssh/id_ed25519"\n')
    secret_bridge.chmod(0o600)
    (config / "targets.d" / "matches-me.toml").write_text(
        'version = 1\nname = "matches-me"\nrole = "bridge"\nprotocol = "direct"\n'
        f'secret_backend = "file"\nsecret_ref = "{secret_bridge}"\n'
    )
    secret_gpu = config / "secrets" / "gpu.toml"
    secret_gpu.write_text('host = "192.0.2.3"\nuser = "nobody"\nport = 9\n')
    secret_gpu.chmod(0o600)
    (config / "targets.d" / "gpu1.toml").write_text(
        'version = 1\nname = "gpu1"\nrole = "compute"\nprotocol = "direct"\n'
        f'reach = ["via:matches-me", "direct"]\n'
        f'secret_backend = "file"\nsecret_ref = "{secret_gpu}"\n'
    )
    monkeypatch.setattr(fleetctl.socket, "gethostname", lambda: "matches-me")
    import argparse

    args = argparse.Namespace(
        config_home=config, state_home=tmp_path / "state", cache_home=tmp_path / "cache",
        json=False, probe=False,
    )
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fleetctl.command_doctor(args)
    assert "matches-me" in buf.getvalue()
    assert "local_target" in buf.getvalue()


def test_doctor_does_not_warn_once_local_target_is_set(fleetctl, tmp_path, monkeypatch):
    config = tmp_path / "cfg"
    for sub in ("targets.d", "protocols.d", "profiles.d", "secrets"):
        (config / sub).mkdir(parents=True)
    (config / "config.toml").write_text('version = 1\n\n[defaults]\nlocal_target = "matches-me"\n')
    (config / "projects.toml").write_text("version = 1\n")
    secret_bridge = config / "secrets" / "bridge.toml"
    secret_bridge.write_text('host = "192.0.2.2"\nuser = "nobody"\nport = 9\nauth = "key"\nidentity_file = "~/.ssh/id_ed25519"\n')
    secret_bridge.chmod(0o600)
    (config / "targets.d" / "matches-me.toml").write_text(
        'version = 1\nname = "matches-me"\nrole = "bridge"\nprotocol = "direct"\n'
        f'secret_backend = "file"\nsecret_ref = "{secret_bridge}"\n'
    )
    secret_gpu = config / "secrets" / "gpu.toml"
    secret_gpu.write_text('host = "192.0.2.3"\nuser = "nobody"\nport = 9\n')
    secret_gpu.chmod(0o600)
    (config / "targets.d" / "gpu1.toml").write_text(
        'version = 1\nname = "gpu1"\nrole = "compute"\nprotocol = "direct"\n'
        'reach = ["via:matches-me", "direct"]\n'
        f'secret_backend = "file"\nsecret_ref = "{secret_gpu}"\n'
    )
    monkeypatch.setattr(fleetctl.socket, "gethostname", lambda: "matches-me")
    import argparse
    import io
    import contextlib

    args = argparse.Namespace(
        config_home=config, state_home=tmp_path / "state", cache_home=tmp_path / "cache",
        json=False, probe=False,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fleetctl.command_doctor(args)
    assert "matches this machine's hostname" not in buf.getvalue()
