"""Control budget for managed clusters (item 6): local token bucket + central authority.

Local bucket: pure math against a tmp_path state file, plus a concurrency
stress test (both threads and separate processes, mirroring
test_concurrency.py) proving the total number of grants across every
concurrent caller never exceeds what the declared burst/rate allows.

Central authority: a real `http.server.ThreadingHTTPServer` bound to
127.0.0.1 in a background thread answers scripted responses; fleetctl talks
to it with `urllib.request` only, per the task.  If binding loopback fails
(unlikely, but the task calls out that a fresh network namespace's `lo` can
come up down), the fixture tries `ip link set lo up` -- a local network
namespace command, not a connection to any host -- and skips only if that
still does not work.

Nothing here ever runs ssh/scp/rsync, and nothing reaches any host other than
127.0.0.1.
"""

from __future__ import annotations

import argparse
import http.server
import json
import multiprocessing
import shutil
import socket as socket_module
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest


# -- local bucket: pure token math --------------------------------------------


def _budget(fleetctl, **overrides):
    defaults = dict(action_per_minute=60.0, burst=3.0, canonical_cluster="site1")
    defaults.update(overrides)
    return fleetctl.ControlBudgetRecord(**defaults)


def test_burst_grants_up_to_the_cap_then_refuses(fleetctl, tmp_path):
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    budget = _budget(fleetctl)
    path = fleetctl.control_budget_state_file(paths, "site1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": 3.0, "updated": time.time()}))

    for _ in range(3):
        result = fleetctl.acquire_local_budget(paths, budget, "action")
        assert result == {"budget_scope": "local"}
    with pytest.raises(fleetctl.BudgetRefused) as excinfo:
        fleetctl.acquire_local_budget(paths, budget, "action")
    assert excinfo.value.scope == "local"
    assert excinfo.value.retry_after > 0
    assert excinfo.value.exit_code == 75


def test_missing_state_file_starts_empty_not_full_burst(fleetctl, tmp_path):
    """'A restart must not refill them to a full burst': a missing file must
    never be read as a fully rested bucket."""
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    budget = _budget(fleetctl, action_per_minute=60.0, burst=3.0)
    state = fleetctl.load_control_budget_state(
        fleetctl.control_budget_state_file(paths, "site1")
    )
    assert state["tokens"] == 0.0
    with pytest.raises(fleetctl.BudgetRefused):
        # tokens=0 and essentially no elapsed time: immediately refused.
        fleetctl.acquire_local_budget(paths, budget, "action")


def test_corrupt_state_file_is_also_read_as_empty(fleetctl, tmp_path):
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    path = fleetctl.control_budget_state_file(paths, "site1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    state = fleetctl.load_control_budget_state(path)
    assert state["tokens"] == 0.0


def test_tokens_refill_over_elapsed_time(fleetctl, tmp_path):
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    budget = _budget(fleetctl, action_per_minute=60.0, burst=3.0)  # 1 token/sec
    path = fleetctl.control_budget_state_file(paths, "site1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": 0.0, "updated": time.time() - 2.5}))
    result = fleetctl.acquire_local_budget(paths, budget, "action")
    assert result == {"budget_scope": "local"}


def test_wall_clock_step_backwards_mints_no_tokens(fleetctl, tmp_path):
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    budget = _budget(fleetctl, action_per_minute=60.0, burst=3.0)
    path = fleetctl.control_budget_state_file(paths, "site1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": 0.0, "updated": time.time() + 1000}))
    with pytest.raises(fleetctl.BudgetRefused):
        fleetctl.acquire_local_budget(paths, budget, "action")


def test_op_class_with_no_declared_rate_is_unlimited(fleetctl, tmp_path):
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    budget = fleetctl.ControlBudgetRecord(
        action_per_minute=60.0, burst=1.0, canonical_cluster="site2"
    )
    for _ in range(5):
        result = fleetctl.acquire_local_budget(paths, budget, "monitor")
        assert result == {"budget_scope": "local"}


def test_budget_wait_blocks_until_a_token_frees_up(fleetctl, tmp_path):
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    budget = _budget(fleetctl, action_per_minute=60.0, burst=3.0)  # 1 token/sec
    path = fleetctl.control_budget_state_file(paths, "site1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": 0.0, "updated": time.time()}))
    start = time.monotonic()
    result = fleetctl.acquire_local_budget(paths, budget, "action", wait=2.0)
    elapsed = time.monotonic() - start
    assert result == {"budget_scope": "local"}
    assert 0.5 <= elapsed <= 2.0


def test_budget_wait_still_refuses_once_exhausted(fleetctl, tmp_path):
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    # A slow rate, so waiting the small budget below cannot possibly earn a token.
    budget = _budget(fleetctl, action_per_minute=1.0, burst=1.0)
    path = fleetctl.control_budget_state_file(paths, "site1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": 0.0, "updated": time.time()}))
    with pytest.raises(fleetctl.BudgetRefused):
        fleetctl.acquire_local_budget(paths, budget, "action", wait=0.2)


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


# -- concurrency: total grants never exceed the token math --------------------


def test_concurrent_threads_never_over_grant(fleetctl, tmp_path):
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    budget = _budget(fleetctl, action_per_minute=0.0001, burst=5.0)  # effectively no refill
    path = fleetctl.control_budget_state_file(paths, "site1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": 5.0, "updated": time.time()}))

    n = 30
    barrier = threading.Barrier(n)
    granted = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            fleetctl.acquire_local_budget(paths, budget, "action")
            with lock:
                granted.append(True)
        except fleetctl.BudgetRefused:
            pass

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 5


def _acquire_one(state_home: str) -> None:
    import importlib.machinery
    import importlib.util
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    loader = importlib.machinery.SourceFileLoader("fleetctl", str(repo_root / "bin" / "fleetctl"))
    spec = importlib.util.spec_from_loader("fleetctl", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fleetctl"] = module
    loader.exec_module(module)
    paths = module.build_paths(
        config_home=Path(state_home) / "c", state_home=Path(state_home), cache_home=Path(state_home) / "ca"
    )
    budget = module.ControlBudgetRecord(
        action_per_minute=0.0001, burst=5.0, canonical_cluster="site1"
    )
    try:
        module.acquire_local_budget(paths, budget, "action")
        (Path(state_home) / f"granted-{os_getpid()}").write_text("1")
    except module.BudgetRefused:
        pass


def os_getpid() -> int:
    import os

    return os.getpid()


def test_concurrent_processes_never_over_grant(tmp_path, fleetctl):
    state_home = tmp_path / "state"
    state_home.mkdir()
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=state_home, cache_home=tmp_path / "ca"
    )
    path = fleetctl.control_budget_state_file(paths, "site1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": 5.0, "updated": time.time()}))

    n = 12
    procs = [
        multiprocessing.Process(target=_acquire_one, args=(str(state_home),))
        for _ in range(n)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    grants = list(state_home.glob("granted-*"))
    assert len(grants) == 5


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

    def log_message(self, *a) -> None:  # noqa: D401 - silence test-server logging
        pass

    def do_POST(self) -> None:  # noqa: N802 - http.server's own naming
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


def test_enforce_control_budget_uses_local_bucket_when_no_authority(fleetctl, tmp_path):
    budget = fleetctl.ControlBudgetRecord(
        action_per_minute=60.0, burst=3.0, canonical_cluster="site1"
    )
    plan = _plan_with_budget(fleetctl, budget, tmp_path)
    # Pre-seed a token so this exercises dispatch-to-the-local-bucket rather
    # than the (separately tested) "a truly fresh bucket starts empty" rule.
    state_path = fleetctl.control_budget_state_file(plan.context.paths, "site1")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"tokens": 1.0, "updated": time.time()}))
    args = argparse.Namespace(budget_wait=0.0)
    result = fleetctl.enforce_control_budget(plan, args, "submit")
    assert result == {"budget_scope": "local"}


def test_enforce_control_budget_redeems_a_held_permit(fleetctl, tmp_path, http_authority, monkeypatch):
    url, token_file = http_authority(
        {"/api/v1/permits/held-1/redeem": {"valid": True, "cluster": "site1", "op_class": "action"}}
    )
    budget = fleetctl.ControlBudgetRecord(
        canonical_cluster="site1", authority_url=url, authority_token_file=token_file
    )
    plan = _plan_with_budget(fleetctl, budget, tmp_path)
    monkeypatch.setenv("FLEETCTL_PERMIT", "held-1")
    args = argparse.Namespace(budget_wait=0.0)
    result = fleetctl.enforce_control_budget(plan, args, "submit")
    assert result == {"budget_scope": "central", "permit_id": "held-1"}


def test_enforce_control_budget_refuses_an_invalid_held_permit(fleetctl, tmp_path, http_authority, monkeypatch):
    url, token_file = http_authority({"/api/v1/permits/held-1/redeem": {"valid": False}})
    budget = fleetctl.ControlBudgetRecord(
        canonical_cluster="site1", authority_url=url, authority_token_file=token_file
    )
    plan = _plan_with_budget(fleetctl, budget, tmp_path)
    monkeypatch.setenv("FLEETCTL_PERMIT", "held-1")
    args = argparse.Namespace(budget_wait=0.0)
    with pytest.raises(fleetctl.BudgetRefused):
        fleetctl.enforce_control_budget(plan, args, "submit")


# -- CLI wiring: a drained budget exits 75 before any connection is attempted -
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
action_per_minute = 60.0
burst = 1
canonical_cluster = "managed-site"
"""


@pytest.fixture()
def budget_sandbox(tmp_path):
    sockets = Path(tempfile.mkdtemp(prefix="fc-", dir="/tmp"))
    config = tmp_path / "cfg"
    for sub in ("targets.d", "protocols.d", "profiles.d", "secrets"):
        (config / sub).mkdir(parents=True)
    (config / "config.toml").write_text(
        f'version = 1\n\n[ssh]\ncontrol_path = "{sockets}/%C"\n'
    )
    (config / "projects.toml").write_text("version = 1\n")
    (config / "protocols.d" / "managed.toml").write_text(PROTOCOL_TOML)
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


def test_exhausted_budget_exits_75_without_connecting(fleetctl_path, budget_sandbox):
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
    assert envelope["details"]["budget_scope"] == "local"
    assert "retry_after" in envelope["details"]
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
    assert "control budget" in (proc.stdout + proc.stderr)
    assert not any(sockets.iterdir()), "a budget refusal opened a connection"
