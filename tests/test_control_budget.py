"""Control budget for managed clusters (item 6): the central permit authority.

A real `http.server.ThreadingHTTPServer` bound to 127.0.0.1 in a background
thread answers scripted responses; fleetctl talks to it with `urllib.request`
only.  If binding loopback fails (unlikely, but a fresh network namespace's `lo`
can come up down), the fixture tries `ip link set lo up` -- a local network
namespace command, not a connection to any host -- and skips only if that
still does not work.

There is no per-machine fallback: a `[control_budget]` without an authority is
a config error, and that is covered here too.

Nothing here ever runs ssh/scp/rsync, and nothing reaches any host other than
127.0.0.1.
"""

from __future__ import annotations

import argparse
import http.server
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


# -- op class resolution -------------------------------------------------------


def test_resolve_op_class_mapping(fleetctl):
    assert fleetctl.resolve_op_class(
        "job", argparse.Namespace(job_command="status")
    ) == "monitor"
    assert fleetctl.resolve_op_class(
        "job", argparse.Namespace(job_command="cancel")
    ) == "action"
    assert fleetctl.resolve_op_class("sync", argparse.Namespace()) == "transfer"
    assert fleetctl.resolve_op_class("submit", argparse.Namespace()) == "action"
    assert fleetctl.resolve_op_class(
        "exec", argparse.Namespace(op_class=None)
    ) == "action"
    assert fleetctl.resolve_op_class(
        "exec", argparse.Namespace(op_class="transfer")
    ) == "transfer"


# -- central authority: local http.server --------------------------------------


def _loopback_available() -> bool:
    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        server.server_close()
        return True
    except OSError:
        pass
    subprocess.run(["ip", "link", "set", "lo", "up"], capture_output=True, timeout=5)
    try:
        server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
        server.server_close()
        return True
    except OSError:
        return False


class _ScriptedHandler(http.server.BaseHTTPRequestHandler):
    responses: dict = {}
    delay: float = 0.0

    def log_message(self, *a) -> None:  # silence test-server logging
        pass

    def do_POST(self) -> None:
        if self.delay:
            time.sleep(self.delay)
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length) if length else b""
        body = self.responses.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture()
def http_authority(tmp_path):
    if not _loopback_available():
        pytest.skip("loopback is not available in this sandbox")

    def make(responses: dict, *, delay: float = 0.0):
        handler_cls = type(
            "Handler", (_ScriptedHandler,), {"responses": responses, "delay": delay}
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        token_file = tmp_path / "token"
        token_file.write_text("sekret-token\n")
        return f"http://127.0.0.1:{port}", str(token_file), server

    servers = []

    def factory(responses: dict, *, delay: float = 0.0):
        url, token_file, server = make(responses, delay=delay)
        servers.append(server)
        return url, token_file

    yield factory
    for server in servers:
        server.shutdown()


def test_authority_grants_a_permit(fleetctl, http_authority):
    url, token_file = http_authority(
        {"/api/v1/permits": {"granted": True, "permit_id": "abc123"}}
    )
    result = fleetctl.authority_request_permit(
        url, token_file, cluster="c1", op_class="action", caller="fleetq"
    )
    assert result == {"granted": True, "permit_id": "abc123"}


def test_authority_denies_with_retry_after(fleetctl, http_authority):
    url, token_file = http_authority(
        {"/api/v1/permits": {"granted": False, "retry_after": 12}}
    )
    with pytest.raises(fleetctl.BudgetRefused) as excinfo:
        fleetctl.authority_request_permit(
            url, token_file, cluster="c1", op_class="action", caller="fleetq"
        )
    assert excinfo.value.scope == "central"
    assert excinfo.value.retry_after == 12
    assert excinfo.value.reason == "denied"


def test_authority_unreachable_denies_by_default(fleetctl, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("sekret\n")
    # Port 1 on loopback: nothing listens there, so the connection is refused
    # immediately -- this is a local, no-network-required failure mode.
    with pytest.raises(fleetctl.BudgetRefused) as excinfo:
        fleetctl.authority_request_permit(
            "http://127.0.0.1:1", str(token_file), cluster="c1",
            op_class="action", caller="fleetq", timeout=1.0,
        )
    assert excinfo.value.reason == "authority_unavailable"


def test_authority_malformed_json_denies_by_default(fleetctl, http_authority):
    url, token_file = http_authority({})  # unregistered path -> 404
    with pytest.raises(fleetctl.BudgetRefused) as excinfo:
        fleetctl.authority_request_permit(
            url, token_file, cluster="c1", op_class="action", caller="fleetq"
        )
    assert excinfo.value.reason == "authority_unavailable"


def test_authority_slow_handler_times_out_and_denies(fleetctl, http_authority):
    url, token_file = http_authority(
        {"/api/v1/permits": {"granted": True, "permit_id": "x"}}, delay=2.0
    )
    start = time.monotonic()
    with pytest.raises(fleetctl.BudgetRefused) as excinfo:
        fleetctl.authority_request_permit(
            url, token_file, cluster="c1", op_class="action", caller="fleetq", timeout=0.5,
        )
    elapsed = time.monotonic() - start
    assert excinfo.value.reason == "authority_unavailable"
    assert elapsed < 1.9  # bounded by the 0.5s timeout, not the 2s delay


def test_authority_redeem_permit_valid(fleetctl, http_authority):
    url, token_file = http_authority(
        {"/api/v1/permits/abc123/redeem": {"valid": True, "cluster": "c1", "op_class": "action"}}
    )
    assert fleetctl.authority_redeem_permit(
        url, token_file, "abc123", cluster="c1", op_class="action"
    ) is True


def test_authority_redeem_permit_invalid(fleetctl, http_authority):
    url, token_file = http_authority({"/api/v1/permits/abc123/redeem": {"valid": False}})
    assert fleetctl.authority_redeem_permit(
        url, token_file, "abc123", cluster="c1", op_class="action"
    ) is False


@pytest.mark.parametrize(
    "answer",
    [
        {"valid": True},  # an authority that does not say which bucket it drew from
        {"valid": True, "cluster": "other", "op_class": "action"},
        {"valid": True, "cluster": "c1", "op_class": "monitor"},
    ],
)
def test_authority_redeem_permit_for_another_bucket_is_false(fleetctl, http_authority, answer):
    url, token_file = http_authority({"/api/v1/permits/abc123/redeem": answer})
    assert fleetctl.authority_redeem_permit(
        url, token_file, "abc123", cluster="c1", op_class="action"
    ) is False


def test_authority_redeem_permit_unreachable_is_false(fleetctl, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("sekret\n")
    assert (
        fleetctl.authority_redeem_permit(
            "http://127.0.0.1:1", str(token_file), "abc123",
            cluster="c1", op_class="action", timeout=1.0,
        )
        is False
    )


# -- enforce_control_budget: FLEETCTL_PERMIT redeem path, and no-budget no-op -


def _plan_with_budget(fleetctl, budget, tmp_path):
    import types

    protocol = fleetctl.ProtocolRecord(
        name="managed", kind="slurm", rule_prefix="M", control_budget=budget
    )
    context = types.SimpleNamespace(
        paths=fleetctl.build_paths(
            config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
        )
    )
    return types.SimpleNamespace(
        protocol=protocol,
        context=context,
        target=types.SimpleNamespace(name="login1"),
    )


def test_enforce_control_budget_is_a_noop_without_control_budget(fleetctl, tmp_path):
    import types

    protocol = fleetctl.ProtocolRecord(name="plain", kind="direct")
    plan = types.SimpleNamespace(protocol=protocol)
    args = argparse.Namespace()
    assert fleetctl.enforce_control_budget(plan, args, "exec") == {}


def _parse(fleetctl, table: str, tmp_path):
    import tomllib

    return fleetctl.parse_control_budget(
        tomllib.loads(table),
        protocol_name="managed",
        what="protocol 'managed'",
        source=tmp_path / "managed.toml",
    )


@pytest.mark.parametrize(
    "table",
    [
        "[control_budget]\ncanonical_cluster = 'site1'\n",
        "[control_budget]\nauthority_url = 'http://127.0.0.1:1'\n",
        "[control_budget]\nauthority_token_file = '/nonexistent'\n",
        # The removed local bucket's keys neither configure a budget nor
        # stand in for an authority.
        "[control_budget]\naction_per_minute = 60.0\nburst = 1\n",
    ],
)
def test_control_budget_without_an_authority_is_a_loud_config_error(
    fleetctl, tmp_path, table
):
    with pytest.raises(fleetctl.FleetError) as caught:
        _parse(fleetctl, table, tmp_path)
    message = str(caught.value)
    assert (
        "control_budget requires authority_url and authority_token_file" in message
        or "unrecognised" in message
    )


def test_local_bucket_keys_are_unknown_keys(fleetctl, tmp_path):
    table = (
        "[control_budget]\nauthority_url = 'http://127.0.0.1:1'\n"
        "authority_token_file = '/t'\naction_per_minute = 60.0\n"
    )
    with pytest.raises(fleetctl.FleetError, match="unrecognised.*action_per_minute"):
        _parse(fleetctl, table, tmp_path)


def test_control_budget_with_an_authority_parses(fleetctl, tmp_path):
    record = _parse(
        fleetctl,
        "[control_budget]\nauthority_url = 'http://127.0.0.1:1'\n"
        "authority_token_file = '/t'\ncanonical_cluster = 'site1'\n",
        tmp_path,
    )
    assert record.canonical_cluster == "site1"
    assert record.authority_url == "http://127.0.0.1:1"


def test_no_control_budget_table_means_no_budget(fleetctl, tmp_path):
    assert _parse(fleetctl, "", tmp_path) is None


def test_enforce_control_budget_redeems_a_held_permit(fleetctl, tmp_path, http_authority, monkeypatch):
    url, token_file = http_authority(
        {"/api/v1/permits/held-1/redeem": {"valid": True, "cluster": "site1", "op_class": "action"}}
    )
    budget = fleetctl.ControlBudgetRecord(
        canonical_cluster="site1", authority_url=url, authority_token_file=token_file
    )
    plan = _plan_with_budget(fleetctl, budget, tmp_path)
    monkeypatch.setenv("FLEETCTL_PERMIT", "held-1")
    args = argparse.Namespace()
    result = fleetctl.enforce_control_budget(plan, args, "submit")
    assert result == {"budget_scope": "central", "permit_id": "held-1"}


def test_enforce_control_budget_refuses_an_invalid_held_permit(fleetctl, tmp_path, http_authority, monkeypatch):
    url, token_file = http_authority({"/api/v1/permits/held-1/redeem": {"valid": False}})
    budget = fleetctl.ControlBudgetRecord(
        canonical_cluster="site1", authority_url=url, authority_token_file=token_file
    )
    plan = _plan_with_budget(fleetctl, budget, tmp_path)
    monkeypatch.setenv("FLEETCTL_PERMIT", "held-1")
    args = argparse.Namespace()
    with pytest.raises(fleetctl.BudgetRefused):
        fleetctl.enforce_control_budget(plan, args, "submit")


# -- CLI wiring: a denied permit exits 75 before any connection is attempted -
#
# `exec` (unlike submit/sync/job) is gated only when the operator names an
# --admin target, so this drives a real (non-dry-run) `fleetctl exec --admin`
# against an unroutable RFC 5737 target (192.0.2.1): the budget check happens
# before plan.connect(), so a refusal here never touches the network, and a
# regression that let it through would hang on a real connection attempt
# instead of failing fast -- exactly what the timeout on the subprocess run
# below is there to catch.

PROTOCOL_TOML = """\
version = 1
name = "managed"
kind = "direct"

[control_budget]
authority_url = "{url}"
authority_token_file = "{token_file}"
canonical_cluster = "managed-site"
"""


@pytest.fixture()
def budget_sandbox(tmp_path, http_authority):
    # An authority that denies everything: the CLI must stop before connecting.
    url, token_file = http_authority(
        {"/api/v1/permits": {"granted": False, "retry_after": 7}}
    )
    sockets = Path(tempfile.mkdtemp(prefix="fc-", dir="/tmp"))
    config = tmp_path / "cfg"
    for sub in ("targets.d", "protocols.d", "profiles.d", "secrets"):
        (config / sub).mkdir(parents=True)
    (config / "config.toml").write_text(
        f'version = 1\n\n[ssh]\ncontrol_path = "{sockets}/%C"\n'
    )
    (config / "projects.toml").write_text("version = 1\n")
    (config / "protocols.d" / "managed.toml").write_text(
        PROTOCOL_TOML.format(url=url, token_file=token_file)
    )
    secret = config / "secrets" / "login.toml"
    secret.write_text('host = "192.0.2.1"\nuser = "nobody"\nport = 9\n')
    secret.chmod(0o600)
    # role="storage": exec needs --admin under the role matrix even though the
    # protocol is plain "direct" (a scheduler-backed "login" role would
    # conflict with a direct-kind protocol, which is a separate coherence
    # check unrelated to what this file is testing).
    (config / "targets.d" / "login.toml").write_text(
        'version = 1\nname = "login"\nrole = "storage"\nprotocol = "managed"\n'
        f'secret_backend = "file"\nsecret_ref = "{secret}"\n'
    )
    yield tmp_path, config, sockets
    shutil.rmtree(sockets, ignore_errors=True)


def test_denied_permit_exits_75_without_connecting(fleetctl_path, budget_sandbox):
    tmp_path, config, sockets = budget_sandbox
    proc = subprocess.run(
        [
            sys.executable, str(fleetctl_path),
            "--config-home", str(config),
            "--state-home", str(tmp_path / "state"),
            "--cache-home", str(tmp_path / "cache"),
            "exec", "login", "--admin", "--op-class", "action", "--", "true",
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 75, proc.stdout + proc.stderr
    assert not any(sockets.iterdir()), "a budget refusal opened a connection"


def test_missing_op_class_still_charges_action_and_notes_it(fleetctl_path, budget_sandbox):
    tmp_path, config, sockets = budget_sandbox
    proc = subprocess.run(
        [
            sys.executable, str(fleetctl_path),
            "--config-home", str(config),
            "--state-home", str(tmp_path / "state"),
            "--cache-home", str(tmp_path / "cache"),
            "exec", "login", "--admin", "--", "true",
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 75, proc.stdout + proc.stderr
    assert "--op-class" in proc.stderr
    assert not any(sockets.iterdir())


def test_budget_json_envelope_reports_budget_refused(fleetctl_path, budget_sandbox):
    tmp_path, config, sockets = budget_sandbox
    proc = subprocess.run(
        [
            sys.executable, str(fleetctl_path),
            "--config-home", str(config),
            "--state-home", str(tmp_path / "state"),
            "--cache-home", str(tmp_path / "cache"),
            "exec", "login", "--admin", "--op-class", "action", "--json", "--", "true",
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert proc.returncode == 75, proc.stdout + proc.stderr
    envelope = json.loads(proc.stdout)
    assert envelope["schema"] == "fleetctl.result/v1"
    assert envelope["outcome"] == "budget_refused"
    assert envelope["may_have_executed"] is False
    assert envelope["details"]["budget_scope"] == "central"
    assert envelope["details"]["retry_after"] == 7
    assert not any(sockets.iterdir())


def _run_cli(fleetctl_path, tmp_path, config, *argv):
    return subprocess.run(
        [
            sys.executable, str(fleetctl_path),
            "--config-home", str(config),
            "--state-home", str(tmp_path / "state"),
            "--cache-home", str(tmp_path / "cache"),
            *argv,
        ],
        capture_output=True, text=True, timeout=15,
    )


@pytest.mark.parametrize("json_flag", [[], ["--json"]])
def test_script_is_charged_like_every_other_control_verb(fleetctl_path, budget_sandbox, json_flag):
    tmp_path, config, sockets = budget_sandbox
    # `script` is refused outright on storage; compute is where it is allowed.
    target = config / "targets.d" / "login.toml"
    target.write_text(target.read_text().replace('role = "storage"', 'role = "compute"'))
    script = tmp_path / "probe.sh"
    script.write_text("#!/bin/sh\ntrue\n")
    proc = _run_cli(fleetctl_path, tmp_path, config, "script", str(script), "--target", "login",
                    *json_flag)
    assert proc.returncode == 75, proc.stdout + proc.stderr
    if json_flag:
        assert json.loads(proc.stdout)["outcome"] == "budget_refused"
    assert not any(sockets.iterdir()), "a budget refusal opened a connection"


def test_fanout_exec_is_charged_per_target(fleetctl_path, budget_sandbox):
    tmp_path, config, sockets = budget_sandbox
    target = config / "targets.d" / "login.toml"
    target.write_text(target.read_text() + 'tags = ["managed"]\n')
    proc = _run_cli(fleetctl_path, tmp_path, config, "exec", "--tag", "managed", "--admin",
                    "--op-class", "action", "--", "true")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "control authority denied a permit" in (proc.stdout + proc.stderr)
    assert not any(sockets.iterdir()), "a budget refusal opened a connection"


def test_sync_source_growth_during_connect_is_refused_before_rsync(fleetctl, monkeypatch, tmp_path):
    """The byte budget is re-checked just before rsync, after the connect."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "a").write_bytes(b"x" * 10)

    def connect():
        (source / "b").write_bytes(b"y" * 4096)  # grows while the route comes up
        return {}

    plan = SimpleNamespace(
        target=SimpleNamespace(name="gpu1", workdir="/work"),
        secret=SimpleNamespace(target="gpu1"), cwd="/work", binding=None, connect=connect,
    )
    monkeypatch.setattr(fleetctl, "build_plan", lambda *_args: plan)
    monkeypatch.setattr(fleetctl, "central_sync_budget_bytes", lambda *_args: 100)
    monkeypatch.setattr(fleetctl, "enforce_control_budget", lambda *_args, **_kw: None)
    monkeypatch.setattr(fleetctl, "run_ssh_command",
                        lambda *_args, **_kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(fleetctl, "build_rsync_transport", lambda *_args, **_kw: "ssh")
    monkeypatch.setattr(fleetctl, "ssh_env", lambda *_args: {})
    monkeypatch.setattr(fleetctl, "run_command",
                        lambda *_args, **_kw: pytest.fail("rsync ran over its budget"))
    args = SimpleNamespace(
        json=False, compress_level=0, direction="push", local_path=str(source),
        remote_path=None, delete=False, force=False, dry_run=False,
    )
    with pytest.raises(fleetctl.FleetError, match="grew to"):
        fleetctl.command_sync(args, object())
