"""The optional role pin is enforced while the local plan is built."""

from __future__ import annotations

import pytest


def parse(fleetctl, *argv):
    return fleetctl.build_parser().parse_args(list(argv))


@pytest.fixture
def direct_context(fleetctl, tmp_path):
    config = tmp_path / "cfg"
    for name in ("targets.d", "protocols.d", "profiles.d", "secrets"):
        (config / name).mkdir(parents=True)
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
    return context, config, tmp_path


@pytest.mark.parametrize(
    "argv",
    [
        ("exec", "box", "--expected-role", "login", "--", "true"),
        ("sync", "push", ".", "--target", "box", "--expected-role", "login"),
        ("sync", "pull", ".", "--target", "box", "--expected-role", "login"),
    ],
)
def test_mismatched_role_refuses_during_plan_before_connection(
    fleetctl, direct_context, monkeypatch, argv
):
    context, _config, _tmp_path = direct_context
    args = parse(fleetctl, *argv)
    monkeypatch.setattr(
        fleetctl, "run_ssh_command",
        lambda *_a, **_kw: pytest.fail("role mismatch must refuse before connection"),
    )
    with pytest.raises(fleetctl.FleetError, match="--expected-role requires 'login'"):
        fleetctl.build_plan(context, args, "exec" if argv[0] == "exec" else "sync")


@pytest.mark.parametrize(
    "argv,verb",
    [
        (("exec", "box", "--expected-role", "workstation", "--", "true"), "exec"),
        (("sync", "push", ".", "--target", "box", "--expected-role", "workstation"), "sync"),
        (("sync", "pull", ".", "--target", "box", "--expected-role", "workstation"), "sync"),
    ],
)
def test_matching_role_builds_plan(fleetctl, direct_context, argv, verb):
    context, _config, _tmp_path = direct_context
    plan = fleetctl.build_plan(context, parse(fleetctl, *argv), verb)
    assert fleetctl.resolve_role(plan.target, plan.protocol) == "workstation"


def test_expected_role_is_rejected_for_sync_transfer(fleetctl, direct_context):
    context, _config, _tmp_path = direct_context
    args = parse(
        fleetctl, "sync", "transfer", "box:/source", "box:/dest",
        "--expected-role", "workstation",
    )
    with pytest.raises(fleetctl.FleetError, match="not supported with `sync transfer`"):
        args.handler(args, context)
