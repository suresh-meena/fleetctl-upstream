"""`--json` result envelope (item 1): fleetctl.result/v1.

Two layers:

* Pure classification unit tests against classify_exception/classify_returncode
  directly -- one per rule in the spec.
* CLI-level tests driven **in-process** (calling `args.handler(args, context)`
  directly, not through a subprocess), because proving each outcome
  (remote_failed, may_have_executed, malformed_response, ...) needs to control
  exactly what the "remote" answered without ever touching a real network --
  so `fleetctl.run_ssh_command`/`run_command`/`control_master_alive` are
  monkeypatched, following the same style tests/test_live_stage.py already
  uses for the sbatch --test-only path.  The fleet config itself still points
  at an unroutable RFC 5737 target (192.0.2.1), so a regression that removed
  a monkeypatch and let a real connection attempt through fails on a hung
  socket rather than silently reaching a host.

Every test asserts on the exact JSON envelope printed to stdout and the
process's returned exit code together, since fleetq (the consumer) reads both.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


# -- pure classification -------------------------------------------------------


def test_classify_returncode_zero_is_ok(fleetctl):
    assert fleetctl.classify_returncode(0) == ("ok", False)


def test_classify_returncode_255_is_may_have_executed(fleetctl):
    assert fleetctl.classify_returncode(255) == ("may_have_executed", True)


def test_classify_returncode_other_nonzero_is_remote_failed(fleetctl):
    assert fleetctl.classify_returncode(1) == ("remote_failed", True)
    assert fleetctl.classify_returncode(127) == ("remote_failed", True)


def test_classify_exception_before_connecting_is_refused(fleetctl):
    attempt = fleetctl.Attempt()
    outcome, executed = fleetctl.classify_exception(attempt, fleetctl.FleetError("no"))
    assert (outcome, executed) == ("refused", False)


def test_classify_exception_while_connecting_but_not_sent_is_transport_failed(fleetctl):
    attempt = fleetctl.Attempt()
    attempt.connecting = True
    outcome, executed = fleetctl.classify_exception(attempt, fleetctl.FleetError("no"))
    assert (outcome, executed) == ("transport_failed", False)


def test_classify_exception_after_sent_is_may_have_executed(fleetctl):
    attempt = fleetctl.Attempt()
    attempt.connecting = True
    attempt.sent = True
    outcome, executed = fleetctl.classify_exception(attempt, fleetctl.FleetError("no"))
    assert (outcome, executed) == ("may_have_executed", True)


def test_classify_exception_budget_refused_is_its_own_outcome_even_after_sent(fleetctl):
    attempt = fleetctl.Attempt()
    attempt.sent = True  # should not matter: BudgetRefused always wins
    exc = fleetctl.BudgetRefused("no tokens", scope="local", retry_after=5.0)
    outcome, executed = fleetctl.classify_exception(attempt, exc)
    assert (outcome, executed) == ("budget_refused", False)


def test_classify_exception_timeout_before_send_is_false(fleetctl):
    attempt = fleetctl.Attempt()
    exc = fleetctl.FleetTimeout("timed out", sent=False)
    assert fleetctl.classify_exception(attempt, exc) == ("timeout", False)


def test_classify_exception_timeout_after_send_is_true(fleetctl):
    attempt = fleetctl.Attempt()
    exc = fleetctl.FleetTimeout("timed out", sent=True)
    assert fleetctl.classify_exception(attempt, exc) == ("timeout", True)


def test_classify_exception_timeout_of_the_primary_command_may_have_executed(fleetctl):
    """run_command() raises every deadline expiry with sent=False; a timeout
    that hit the primary command after it was dispatched must still say the
    remote side may have run it, or a caller would safely-but-wrongly retry."""
    attempt = fleetctl.Attempt()
    attempt.connecting = True
    attempt.sent = True
    exc = fleetctl.FleetTimeout("--timeout budget exhausted running ssh", sent=False)
    assert fleetctl.classify_exception(attempt, exc) == ("timeout", True)


def test_exec_timeout_during_the_remote_command_may_have_executed(fleetctl, direct_context, monkeypatch):
    context, config, tmp_path = direct_context

    def fake(secret, *, allocate_tty, remote_command, capture_output=False, **kw):
        raise fleetctl.FleetTimeout("--timeout budget exhausted running ssh", sent=False)

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path, ["exec", "box", "--json", "--", "true"]
    )
    envelope = one_json_object(out)
    assert code == 124
    assert envelope["outcome"] == "timeout"
    assert envelope["may_have_executed"] is True


def test_classify_exception_handles_an_unexpected_bug_the_same_way(fleetctl):
    """No ninth "internal error" bucket exists: an unrelated bug is still
    classified from the same Attempt state as any FleetError would be."""
    attempt = fleetctl.Attempt()
    attempt.connecting = True
    outcome, executed = fleetctl.classify_exception(attempt, AttributeError("boom"))
    assert (outcome, executed) == ("transport_failed", False)


def test_build_result_envelope_schema_and_ok_field(fleetctl):
    envelope = fleetctl.build_result_envelope(
        verb="exec", target="t", route="direct", outcome="ok", may_have_executed=False,
        exit_code=0, remote_exit_code=0, stdout="hi", stderr="", stdout_truncated=False,
        stderr_truncated=False, duration_s=1.23456789, error=None, details={"x": 1},
    )
    assert envelope["schema"] == "fleetctl.result/v1"
    assert envelope["ok"] is True
    assert envelope["duration_s"] == round(1.23456789, 6)
    assert envelope["details"] == {"x": 1}
    assert envelope["error"] is None


def test_build_result_envelope_error_field_shape(fleetctl):
    envelope = fleetctl.build_result_envelope(
        verb="exec", target=None, route=None, outcome="refused", may_have_executed=False,
        exit_code=2, remote_exit_code=None, stdout="", stderr="", stdout_truncated=False,
        stderr_truncated=False, duration_s=0.0, error=fleetctl.FleetError("nope"),
    )
    assert envelope["error"] == {"class": "FleetError", "message": "nope"}
    assert envelope["ok"] is False


# -- CLI-level, in-process, monkeypatched transport ---------------------------


def short_socket_config(config: Path, request) -> None:
    """pytest's tmp_path is long enough that `<cache>/fleet/mux/%C` trips the
    ControlPath length guard, so point the sockets at a short /tmp dir, as
    test_control_budget.py and test_inline_payload.py do."""
    sockets = Path(tempfile.mkdtemp(prefix="fc-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(sockets, ignore_errors=True))
    (config / "config.toml").write_text(f'version = 1\n\n[ssh]\ncontrol_path = "{sockets}/%C"\n')



@pytest.fixture()
def direct_context(fleetctl, tmp_path, monkeypatch, request):
    """A one-target fleet (direct protocol, workstation role) at 192.0.2.1
    (RFC 5737, unroutable), with the control-master probe faked so nothing
    ever actually dials out."""
    config = tmp_path / "cfg"
    for sub in ("targets.d", "protocols.d", "profiles.d", "secrets"):
        (config / sub).mkdir(parents=True)
    short_socket_config(config, request)
    (config / "projects.toml").write_text("version = 1\n")
    secret = config / "secrets" / "login.toml"
    secret.write_text('host = "192.0.2.1"\nuser = "nobody"\nport = 9\n')
    secret.chmod(0o600)
    (config / "targets.d" / "box.toml").write_text(
        'version = 1\nname = "box"\nrole = "workstation"\nprotocol = "direct"\n'
        f'secret_backend = "file"\nsecret_ref = "{secret}"\n'
    )
    context = fleetctl.FleetContext.load(
        config_home=config, state_home=tmp_path / "state", cache_home=tmp_path / "cache"
    )
    monkeypatch.setattr(fleetctl, "control_master_alive", lambda secret, runtime: True)
    return context, config, tmp_path


@pytest.fixture()
def slurm_context(fleetctl, tmp_path, monkeypatch, request):
    """A one-target fleet on a Slurm-kind protocol, for `submit`/`job status`/
    `job cancel` tests."""
    config = tmp_path / "cfg"
    for sub in ("targets.d", "protocols.d", "profiles.d", "secrets"):
        (config / sub).mkdir(parents=True)
    short_socket_config(config, request)
    (config / "projects.toml").write_text("version = 1\n")
    (config / "protocols.d" / "kiac.toml").write_text(
        "version = 1\nname = \"kiac\"\nkind = \"slurm\"\nrule_prefix = \"KIAC\"\n\n"
        "[[queue]]\nname = \"short\"\npartition = \"short\"\ndefault = true\n"
        "account = \"research\"\ntime_limit = \"00:10:00\"\nmax_time = \"24:00:00\"\n"
        "evidence = \"verified-live\"\n"
    )
    (config / "profiles.d" / "slurm-batch.toml").write_text(
        "version = 1\nname = \"slurm-batch\"\n"
        "job_id_pattern = \"Submitted batch job (?P<job_id>\\\\d+)\"\n"
        "submit_command = [\"sbatch\", \"{script}\"]\n"
        "status_command = [\"squeue\", \"--jobs\", \"{job_id}\"]\n"
        "cancel_command = [\"scancel\", \"{job_id}\"]\n"
        "logs_command = [\"sacct\", \"--jobs\", \"{job_id}\"]\n"
    )
    secret = config / "secrets" / "login.toml"
    secret.write_text('host = "192.0.2.1"\nuser = "nobody"\nport = 9\n')
    secret.chmod(0o600)
    (config / "targets.d" / "login.toml").write_text(
        'version = 1\nname = "login"\nrole = "login"\nprotocol = "kiac"\n'
        f'workdir = "/home/nobody"\nsecret_backend = "file"\nsecret_ref = "{secret}"\n'
    )
    context = fleetctl.FleetContext.load(
        config_home=config, state_home=tmp_path / "state", cache_home=tmp_path / "cache"
    )
    monkeypatch.setattr(fleetctl, "control_master_alive", lambda secret, runtime: True)
    return context, config, tmp_path


def run_handler(fleetctl, context, config, tmp_path, argv):
    parser = fleetctl.build_parser()
    args = parser.parse_args(argv)
    args.config_home = config
    args.state_home = tmp_path / "state"
    args.cache_home = tmp_path / "cache"
    buf = io.StringIO()
    errbuf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(errbuf):
        code = args.handler(args, context)
    return code, buf.getvalue(), errbuf.getvalue()


def one_json_object(stdout: str) -> dict:
    """Assert STDOUT is exactly one JSON document and return it parsed."""
    decoder = json.JSONDecoder()
    obj, end = decoder.raw_decode(stdout.strip())
    assert stdout.strip()[end:].strip() == "", "stdout carried more than one JSON object"
    return obj


# -- exec ----------------------------------------------------------------------


def test_exec_ok(fleetctl, direct_context, monkeypatch):
    context, config, tmp_path = direct_context

    def fake(secret, *, allocate_tty, remote_command, capture_output=False, **kw):
        return subprocess.CompletedProcess(["ssh"], 0, "hello\n", "")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path, ["exec", "box", "--json", "--", "echo", "hi"]
    )
    envelope = one_json_object(out)
    assert code == 0
    assert envelope["schema"] == "fleetctl.result/v1"
    assert envelope["verb"] == "exec"
    assert envelope["target"] == "box"
    assert envelope["outcome"] == "ok"
    assert envelope["ok"] is True
    assert envelope["may_have_executed"] is False
    assert envelope["exit_code"] == 0
    assert envelope["remote_exit_code"] == 0
    assert envelope["stdout"] == "hello\n"


def test_exec_remote_failed(fleetctl, direct_context, monkeypatch):
    context, config, tmp_path = direct_context

    def fake(secret, *, allocate_tty, remote_command, capture_output=False, **kw):
        return subprocess.CompletedProcess(["ssh"], 3, "", "boom\n")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path, ["exec", "box", "--json", "--", "false"]
    )
    envelope = one_json_object(out)
    assert code == 3
    assert envelope["outcome"] == "remote_failed"
    assert envelope["may_have_executed"] is True
    assert envelope["remote_exit_code"] == 3
    assert envelope["ok"] is False


def test_exec_ssh_255_is_may_have_executed(fleetctl, direct_context, monkeypatch):
    context, config, tmp_path = direct_context

    def fake(secret, *, allocate_tty, remote_command, capture_output=False, **kw):
        return subprocess.CompletedProcess(["ssh"], 255, "", "Connection closed\n")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path, ["exec", "box", "--json", "--", "true"]
    )
    envelope = one_json_object(out)
    assert code == 255
    assert envelope["outcome"] == "may_have_executed"
    assert envelope["may_have_executed"] is True


def test_exec_refused_before_any_connection(fleetctl, direct_context, monkeypatch):
    context, config, tmp_path = direct_context

    def boom(*a, **k):
        raise AssertionError("must not connect for a refused exec")

    monkeypatch.setattr(fleetctl, "run_ssh_command", boom)
    monkeypatch.setattr(fleetctl, "control_master_alive", boom)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path, ["exec", "no-such-target", "--json", "--", "true"]
    )
    envelope = one_json_object(out)
    assert envelope["outcome"] == "refused"
    assert envelope["target"] is None
    assert envelope["may_have_executed"] is False
    assert code == envelope["exit_code"] == 2


def test_exec_transport_failed_when_control_master_never_comes_up(
    fleetctl, direct_context, monkeypatch
):
    context, config, tmp_path = direct_context

    def boom(*a, **k):
        raise AssertionError("must not send once the control master failed")

    monkeypatch.setattr(fleetctl, "run_ssh_command", boom)
    monkeypatch.setattr(fleetctl, "control_master_alive", lambda secret, runtime: False)
    monkeypatch.setattr(
        fleetctl, "open_control_master", lambda secret, runtime, route: "no route to host"
    )
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path, ["exec", "box", "--json", "--", "true"]
    )
    envelope = one_json_object(out)
    assert envelope["outcome"] == "transport_failed"
    assert envelope["target"] == "box"
    assert envelope["may_have_executed"] is False


def test_exec_json_dry_run_never_connects(fleetctl, direct_context, monkeypatch):
    context, config, tmp_path = direct_context

    def boom(*a, **k):
        raise AssertionError("dry-run must never connect")

    monkeypatch.setattr(fleetctl, "run_ssh_command", boom)
    monkeypatch.setattr(fleetctl, "control_master_alive", boom)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path,
        ["exec", "box", "--json", "--dry-run", "--", "true"],
    )
    envelope = one_json_object(out)
    assert code == 0
    assert envelope["outcome"] == "ok"
    assert envelope["may_have_executed"] is False
    assert envelope["details"]["dry_run"] is True


def test_exec_json_refuses_fanout(fleetctl, direct_context):
    context, config, tmp_path = direct_context
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path, ["exec", "--json", "--all", "--", "true"]
    )
    assert code == 2
    envelope = one_json_object(out)
    assert envelope["outcome"] == "refused"
    assert "--all" in envelope["error"]["message"]


def test_exec_stdout_carries_nothing_but_the_envelope(fleetctl, direct_context, monkeypatch):
    context, config, tmp_path = direct_context

    def fake(secret, *, allocate_tty, remote_command, capture_output=False, **kw):
        return subprocess.CompletedProcess(["ssh"], 0, "payload output\n", "")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path, ["exec", "box", "--json", "--", "echo", "x"]
    )
    # The remote's own stdout ("payload output") must land inside the
    # envelope's "stdout" field, never interleaved with it on the real stdout.
    one_json_object(out)  # raises if stdout is not exactly one JSON document


# -- capture limits -------------------------------------------------------------


def test_exec_capture_limit_truncates_and_flags_it(fleetctl, direct_context, monkeypatch):
    context, config, tmp_path = direct_context
    big = "x" * 1000

    def fake(secret, *, allocate_tty, remote_command, capture_output=False,
             stdout_limit=None, stderr_limit=None, **kw):
        # Exercise the real run_command capture-limit machinery rather than
        # faking pre-truncated output, so this proves the plumbing (the
        # --capture-limit flag reaching run_ssh_command's stdout_limit) works.
        return fleetctl.run_command(
            ["python3", "-c", f"import sys; sys.stdout.write({big!r})"],
            capture_output=True, stdout_limit=stdout_limit, stderr_limit=stderr_limit,
        )

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path,
        ["exec", "box", "--json", "--capture-limit", "10", "--", "true"],
    )
    envelope = one_json_object(out)
    assert len(envelope["stdout"]) == 10
    assert envelope["stdout_truncated"] is True


# -- script ----------------------------------------------------------------------


def test_script_ok(fleetctl, direct_context, monkeypatch, tmp_path):
    context, config, state_root = direct_context
    monkeypatch.setattr(
        fleetctl, "stage_remote_script",
        lambda secret, profile, local_script, *, ssh_runtime=None: "/staged/x.sh",
    )

    def fake(secret, *, allocate_tty, remote_command, capture_output=False, **kw):
        return subprocess.CompletedProcess(["ssh"], 0, "ran\n", "")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake)
    script = state_root / "job.sh"
    script.write_text("#!/bin/sh\necho ran\n")
    code, out, err = run_handler(
        fleetctl, context, config, state_root, ["script", str(script), "--target", "box", "--json"]
    )
    envelope = one_json_object(out)
    assert code == 0
    assert envelope["verb"] == "script"
    assert envelope["outcome"] == "ok"
    assert envelope["stdout"] == "ran\n"


def test_script_dry_run_reports_staged_path_without_staging(fleetctl, direct_context, monkeypatch):
    context, config, tmp_path = direct_context

    def boom(*a, **k):
        raise AssertionError("dry-run must not stage")

    monkeypatch.setattr(fleetctl, "stage_remote_script", boom)
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/sh\necho ran\n")
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path,
        ["script", str(script), "--target", "box", "--json", "--dry-run"],
    )
    envelope = one_json_object(out)
    assert code == 0
    assert envelope["details"]["dry_run"] is True
    assert "staged_as" in envelope["details"]


# -- sync ------------------------------------------------------------------------


def test_sync_push_ok(fleetctl, direct_context, monkeypatch, tmp_path):
    context, config, state_root = direct_context
    local_dir = tmp_path / "src"
    local_dir.mkdir()

    def fake_run_command(command, *, env=None, capture_output=False, **kw):
        if command[0] == "rsync":
            return subprocess.CompletedProcess(command, 0, "sent 10 bytes\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(fleetctl, "run_command", fake_run_command)

    def fake_run_ssh(secret, *, allocate_tty, remote_command, capture_output=False, **kw):
        return subprocess.CompletedProcess(["ssh"], 0, "", "")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake_run_ssh)
    code, out, err = run_handler(
        fleetctl, context, config, state_root,
        ["sync", "push", str(local_dir), "/remote/dst", "--target", "box", "--json"],
    )
    envelope = one_json_object(out)
    assert code == 0
    assert envelope["verb"] == "sync"
    assert envelope["outcome"] == "ok"
    assert envelope["details"]["direction"] == "push"


def test_sync_remote_failed(fleetctl, direct_context, monkeypatch, tmp_path):
    context, config, state_root = direct_context
    local_dir = tmp_path / "src"
    local_dir.mkdir()

    def fake_run_command(command, *, env=None, capture_output=False, **kw):
        if command[0] == "rsync":
            return subprocess.CompletedProcess(command, 23, "", "rsync error\n")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(fleetctl, "run_command", fake_run_command)
    monkeypatch.setattr(
        fleetctl, "run_ssh_command",
        lambda secret, *, allocate_tty, remote_command, capture_output=False, **kw:
            subprocess.CompletedProcess(["ssh"], 0, "", ""),
    )
    code, out, err = run_handler(
        fleetctl, context, config, state_root,
        ["sync", "push", str(local_dir), "/remote/dst", "--target", "box", "--json"],
    )
    envelope = one_json_object(out)
    assert code == 23
    assert envelope["outcome"] == "remote_failed"
    assert envelope["may_have_executed"] is True


def test_sync_mkdir_staging_failure_is_transport_failed(fleetctl, direct_context, monkeypatch, tmp_path):
    context, config, state_root = direct_context
    local_dir = tmp_path / "src"
    local_dir.mkdir()

    def rsync_must_not_run(command, *, env=None, capture_output=False, **kw):
        assert command[0] != "rsync", "rsync must not run once mkdir staging failed"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(fleetctl, "run_command", rsync_must_not_run)
    monkeypatch.setattr(
        fleetctl, "run_ssh_command",
        lambda secret, *, allocate_tty, remote_command, capture_output=False, **kw:
            subprocess.CompletedProcess(["ssh"], 1, "", "mkdir: permission denied\n"),
    )
    code, out, err = run_handler(
        fleetctl, context, config, state_root,
        ["sync", "push", str(local_dir), "/remote/dst", "--target", "box", "--json"],
    )
    envelope = one_json_object(out)
    assert envelope["outcome"] == "transport_failed"
    assert envelope["may_have_executed"] is False


# -- job status / job cancel ----------------------------------------------------


def test_job_status_ok(fleetctl, slurm_context, monkeypatch):
    context, config, tmp_path = slurm_context

    def fake(secret, *, allocate_tty, remote_command, capture_output=False, **kw):
        return subprocess.CompletedProcess(["ssh"], 0, "JOBID ST\n12345 R\n", "")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path,
        ["job", "status", "12345", "--target", "login", "--json"],
    )
    envelope = one_json_object(out)
    assert code == 0
    assert envelope["verb"] == "job"
    assert envelope["outcome"] == "ok"
    assert envelope["details"]["job_id"] == "12345"


def test_job_status_refuses_fanout_with_json(fleetctl, slurm_context):
    context, config, tmp_path = slurm_context
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path,
        ["job", "status", "12345", "--json", "--all"],
    )
    envelope = one_json_object(out)
    assert envelope["outcome"] == "refused"
    assert code == 2


def test_job_cancel_dry_run(fleetctl, slurm_context):
    context, config, tmp_path = slurm_context
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path,
        ["job", "cancel", "12345", "--target", "login", "--json", "--dry-run"],
    )
    envelope = one_json_object(out)
    assert code == 0
    assert envelope["details"]["dry_run"] is True
    assert envelope["details"]["job_id"] == "12345"


def test_job_cancel_remote_failed(fleetctl, slurm_context, monkeypatch):
    context, config, tmp_path = slurm_context

    def fake(secret, *, allocate_tty, remote_command, capture_output=False, **kw):
        return subprocess.CompletedProcess(["ssh"], 1, "", "Invalid job id specified\n")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path,
        ["job", "cancel", "99999", "--target", "login", "--json"],
    )
    envelope = one_json_object(out)
    assert code == 1
    assert envelope["outcome"] == "remote_failed"


# -- submit ----------------------------------------------------------------------


def test_submit_ok_records_job_id_and_ledger(fleetctl, slurm_context, monkeypatch):
    context, config, tmp_path = slurm_context
    script = tmp_path / "train.sh"
    script.write_text("#!/bin/bash\nset -euo pipefail\npython3 train.py\n")

    calls = []

    def fake_run_ssh(secret, *, allocate_tty, remote_command, capture_output=False, **kw):
        calls.append(remote_command)
        if "mkdir" in remote_command:
            return subprocess.CompletedProcess(["ssh"], 0, "", "")
        return subprocess.CompletedProcess(
            ["ssh"], 0, "Submitted batch job 555\n", ""
        )

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake_run_ssh)
    monkeypatch.setattr(
        fleetctl, "stage_remote_script",
        lambda secret, profile, local_script, *, ssh_runtime=None: "/staged/" + local_script.name,
    )
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path,
        ["submit", str(script), "--target", "login", "--json", "--queue", "short"],
    )
    envelope = one_json_object(out)
    assert code == 0
    assert envelope["verb"] == "submit"
    assert envelope["outcome"] == "ok"
    assert envelope["details"]["job_id"] == "555"
    assert envelope["details"]["ledger_recorded"] is True
    ledger = fleetctl.read_jsonl(fleetctl.jobs_ledger_file(context.paths))
    assert ledger and ledger[-1]["job_id"] == "555"


def test_submit_malformed_response(fleetctl, slurm_context, monkeypatch):
    context, config, tmp_path = slurm_context
    script = tmp_path / "train.sh"
    script.write_text("#!/bin/bash\nset -euo pipefail\npython3 train.py\n")
    monkeypatch.setattr(
        fleetctl, "stage_remote_script",
        lambda secret, profile, local_script, *, ssh_runtime=None: "/staged/" + local_script.name,
    )

    def fake_run_ssh(secret, *, allocate_tty, remote_command, capture_output=False, **kw):
        if "mkdir" in remote_command:
            return subprocess.CompletedProcess(["ssh"], 0, "", "")
        return subprocess.CompletedProcess(["ssh"], 0, "no job id in here\n", "")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake_run_ssh)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path,
        ["submit", str(script), "--target", "login", "--json", "--queue", "short"],
    )
    envelope = one_json_object(out)
    assert code == 0  # sbatch itself succeeded
    assert envelope["outcome"] == "malformed_response"
    assert envelope["may_have_executed"] is True
    assert envelope["details"]["job_id"] is None


def test_submit_refused_by_preflight_never_connects(fleetctl, slurm_context, monkeypatch):
    context, config, tmp_path = slurm_context
    # h200: not declared on this protocol's queue list at all -> KIAC011.
    script = tmp_path / "train.sh"
    script.write_text("#!/bin/bash\nset -euo pipefail\npython3 train.py\n")

    def boom(*a, **k):
        raise AssertionError("a preflight refusal must never stage or connect")

    monkeypatch.setattr(fleetctl, "stage_remote_script", boom)
    monkeypatch.setattr(fleetctl, "run_ssh_command", boom)
    monkeypatch.setattr(fleetctl, "control_master_alive", boom)
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path,
        ["submit", str(script), "--target", "login", "--json", "--queue", "does-not-exist"],
    )
    envelope = one_json_object(out)
    assert envelope["outcome"] == "refused"
    assert envelope["may_have_executed"] is False
    assert code == envelope["exit_code"]


def test_submit_dry_run_reports_inlined_payload(fleetctl, slurm_context):
    context, config, tmp_path = slurm_context
    script = tmp_path / "train.sh"
    script.write_text("#!/bin/bash\nset -euo pipefail\npython3 train.py\n")
    code, out, err = run_handler(
        fleetctl, context, config, tmp_path,
        ["submit", str(script), "--target", "login", "--json", "--queue", "short", "--dry-run"],
    )
    envelope = one_json_object(out)
    assert code == 0
    assert envelope["details"]["dry_run"] is True
    assert envelope["details"]["staged_as"] == "(inlined into wrapper)"
    assert "FLEETCTL_PAYLOAD_" in envelope["details"]["rendered_wrapper"]
