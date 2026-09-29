"""The `sync --delete` guard decides on what a path names, not how it is typed.

`rsync --delete` removes everything at the destination the source lacks, so a
destination that is really a home directory empties it.  The guard used to
compare spellings: it refused `~` and `$HOME`, and allowed `~/.`,
`/home/<user>` and `../..` from a project root -- each a home directory.  Every
row below goes through `resolve_remote_path` first, exactly as `sync` does, so
relative spellings are tested in the form the guard actually receives.
"""

from __future__ import annotations

import pytest

WORKDIR = "/home/u/work"
PROJECT_ROOT = "/home/u/work/proj"

REFUSED_EVEN_WITH_FORCE = [
    "~", "~/", "~/.", "~/..", "$HOME", "${HOME}", "`pwd`",
    "/", "/.", "//", "/storage", "/home",
    "/home/u", "/home/u/", "/home/u/work/..", "/rhome/u", "/Users/u", "/root",
    "/root/.ssh", "/etc/ssh", "/var/lib", "/usr/local/bin",
    "../..",
]
# `..` from the project root is the workdir itself, not something above it.
WORKDIR_SPELLINGS = [
    "/home/u/work", "/home/u/work/", "/home/u/work/.", "/home/u/work/proj/..", "..",
]
ALLOWED = [
    ".", "sub/dir", "/home/u/work/proj", "/home/u/work/other",
    "~/work/proj", "/storage/u/proj", "~/work/proj/../proj2",
]


@pytest.fixture()
def target(fleetctl):
    return fleetctl.TargetRecord(
        name="t", role="workstation", protocol="direct", workdir=WORKDIR
    )


def guard(fleetctl, target, raw, *, force=False, direction="push"):
    resolved = fleetctl.resolve_remote_path(raw, base=PROJECT_ROOT)
    fleetctl.ensure_sync_delete_allowed(
        resolved, target, direction=direction, force=force
    )


@pytest.mark.parametrize("raw", REFUSED_EVEN_WITH_FORCE)
@pytest.mark.parametrize("force", [False, True])
def test_home_root_and_ancestors_are_refused(fleetctl, target, raw, force):
    with pytest.raises(fleetctl.FleetError):
        guard(fleetctl, target, raw, force=force)


@pytest.mark.parametrize("raw", WORKDIR_SPELLINGS)
def test_every_spelling_of_the_workdir_needs_force(fleetctl, target, raw):
    with pytest.raises(fleetctl.FleetError) as exc:
        guard(fleetctl, target, raw)
    assert "--force" in str(exc.value)
    guard(fleetctl, target, raw, force=True)


@pytest.mark.parametrize("raw", ALLOWED)
def test_project_paths_are_allowed(fleetctl, target, raw):
    guard(fleetctl, target, raw)


def test_an_ancestor_of_the_workdir_names_why(fleetctl):
    deep = fleetctl.TargetRecord(
        name="t", role="workstation", protocol="direct", workdir="/storage/lab/u/work"
    )
    with pytest.raises(fleetctl.FleetError) as exc:
        fleetctl.ensure_sync_delete_allowed(
            "/storage/lab/u", deep, direction="push", force=True
        )
    assert "contains the host-level workdir" in str(exc.value)


def test_pull_delete_still_needs_force(fleetctl, target):
    with pytest.raises(fleetctl.FleetError):
        guard(fleetctl, target, "sub/dir", direction="pull")
    guard(fleetctl, target, "sub/dir", direction="pull", force=True)


@pytest.mark.parametrize("path", ["~alice", "~alice/project", "~root/.ssh"])
def test_named_user_home_paths_are_refused(fleetctl, target, path):
    with pytest.raises(fleetctl.FleetError, match="named-user home paths"):
        fleetctl.ensure_sync_delete_allowed(
            path, target, direction="push", force=True
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("~", ("~", ".")), ("~/", ("~", ".")), ("~/.", ("~", ".")),
        ("~/a/../b", ("~", "b")), ("/home/u/", ("/", "/home/u")),
        ("//x//y/", ("/", "/x/y")), ("/a/./b/..", ("/", "/a")), ("a/../..", (".", "..")),
    ],
)
def test_normalize_remote_path(fleetctl, raw, expected):
    assert fleetctl.normalize_remote_path(raw) == expected


@pytest.mark.parametrize("path", ["/", "/home", "/etc/ssh", "/usr/local/bin"])
def test_local_pull_delete_refuses_protected_paths_even_with_force(fleetctl, path):
    with pytest.raises(fleetctl.FleetError):
        fleetctl.ensure_local_sync_delete_allowed(path)


def test_local_pull_delete_resolves_symlink_aliases(fleetctl, tmp_path):
    alias = tmp_path / "home-alias"
    alias.symlink_to("/home", target_is_directory=True)
    with pytest.raises(fleetctl.FleetError):
        fleetctl.ensure_local_sync_delete_allowed(alias)


def test_local_guard_rejects_custom_home_and_ancestors(fleetctl, monkeypatch, tmp_path):
    home = tmp_path / "custom" / "user"
    home.mkdir(parents=True)
    monkeypatch.setattr(fleetctl.Path, "home", classmethod(lambda cls: home))
    for path in (home, home.parent):
        with pytest.raises(fleetctl.FleetError):
            fleetctl.ensure_local_sync_delete_allowed(path)


@pytest.mark.parametrize("name", [".ssh", ".gnupg", ".config", ".local", ".cache"])
def test_local_guard_rejects_protected_home_dirs_and_aliases(
    fleetctl, monkeypatch, tmp_path, name
):
    home = tmp_path / "custom-home"
    protected = home / name
    (protected / "nested").mkdir(parents=True)
    alias = tmp_path / "config-alias"
    alias.symlink_to(protected, target_is_directory=True)
    monkeypatch.setattr(fleetctl.Path, "home", classmethod(lambda cls: home))
    for path in (protected, protected / "nested", alias):
        with pytest.raises(fleetctl.FleetError):
            fleetctl.ensure_local_sync_delete_allowed(path)


def test_local_guard_resolves_protected_home_root_symlinks(
    fleetctl, monkeypatch, tmp_path
):
    home = tmp_path / "home"
    outside = tmp_path / "ssh-data"
    home.mkdir()
    (outside / "nested").mkdir(parents=True)
    (home / ".ssh").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(fleetctl.Path, "home", classmethod(lambda cls: home))
    with pytest.raises(fleetctl.FleetError, match="protected home directory"):
        fleetctl.ensure_local_sync_delete_allowed(home / ".ssh" / "nested")


def test_remote_delete_preflight_resolves_destination_before_guard(
    fleetctl, monkeypatch, target
):
    from types import SimpleNamespace

    calls = []

    def fake_ssh(secret, **kwargs):
        calls.append(kwargs["remote_command"])
        return SimpleNamespace(returncode=0, stdout="/home/u\n/home/u\n", stderr="")

    monkeypatch.setattr(fleetctl, "run_ssh_command", fake_ssh)
    secret = SimpleNamespace(target="t")
    canonical, remote_home, _ = fleetctl.resolve_remote_delete_path(
        secret, "/work/link", {}
    )
    assert "realpath" in calls[0]
    with pytest.raises(fleetctl.FleetError):
        fleetctl.ensure_remote_home_not_target(canonical, remote_home, target)
    with pytest.raises(fleetctl.FleetError):
        fleetctl.ensure_sync_delete_allowed(
            canonical, target, direction="push", force=True
        )


@pytest.mark.parametrize("json_mode", [False, True])
def test_destructive_sync_uses_checked_canonical_destination(fleetctl, monkeypatch, tmp_path, json_mode):
    from types import SimpleNamespace

    source = tmp_path / "source"
    source.mkdir()
    target = fleetctl.TargetRecord(name="box", role="workstation", protocol="direct",
                                   workdir="/home/u/work")
    secret = SimpleNamespace(target="box")
    plan = SimpleNamespace(target=target, secret=secret, cwd="/home/u/work/project",
                           route="direct", binding=None, connect=lambda: {})
    monkeypatch.setattr(fleetctl, "build_plan", lambda *_args: plan)
    monkeypatch.setattr(fleetctl, "central_sync_budget_bytes", lambda *_args: None)
    monkeypatch.setattr(fleetctl, "enforce_control_budget", lambda *_args, **_kw: None)
    monkeypatch.setattr(fleetctl, "resolve_remote_delete_path",
                        lambda *_args: ("/scratch/u/project", "/home/u", "/home/u/work"))
    monkeypatch.setattr(fleetctl, "run_ssh_command",
                        lambda *_args, **_kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(fleetctl, "build_rsync_transport", lambda *_args, **_kw: "ssh")
    monkeypatch.setattr(fleetctl, "ssh_env", lambda *_args: {})
    commands = []

    def run(argv, **_kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="",
                               stdout_truncated=False, stderr_truncated=False)

    monkeypatch.setattr(fleetctl, "run_command", run)
    args = SimpleNamespace(json=json_mode, direction="push", compress_level=0, delete=True, force=False,
                           local_path=str(source), remote_path="~/work/project", dry_run=False)
    assert fleetctl.command_sync(args, None) == 0
    assert commands[0][-1] == "box:/scratch/u/project"


@pytest.mark.parametrize("json_mode", [False, True])
def test_destructive_pull_uses_checked_local_destination(fleetctl, monkeypatch, tmp_path, json_mode):
    from types import SimpleNamespace

    safe = tmp_path / "safe"
    other = tmp_path / "other"
    safe.mkdir()
    other.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(safe, target_is_directory=True)
    target = fleetctl.TargetRecord(name="box", role="workstation", protocol="direct",
                                   workdir="/home/u/work")
    plan = SimpleNamespace(target=target, secret=SimpleNamespace(target="box"),
                           cwd="/home/u/work/project", route="direct", binding=None,
                           connect=lambda: {})
    monkeypatch.setattr(fleetctl, "build_plan", lambda *_args: plan)
    monkeypatch.setattr(fleetctl, "central_sync_budget_bytes", lambda *_args: None)
    monkeypatch.setattr(fleetctl, "enforce_control_budget", lambda *_args, **_kw: None)

    def after_local_check(*_args):
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        return "/scratch/u/project", "/home/u", "/home/u/work"

    monkeypatch.setattr(fleetctl, "resolve_remote_delete_path", after_local_check)
    monkeypatch.setattr(fleetctl, "build_rsync_transport", lambda *_args, **_kw: "ssh")
    monkeypatch.setattr(fleetctl, "ssh_env", lambda *_args: {})
    commands = []

    def run(argv, **_kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="",
                               stdout_truncated=False, stderr_truncated=False)

    monkeypatch.setattr(fleetctl, "run_command", run)
    args = SimpleNamespace(json=json_mode, direction="pull", compress_level=0,
                           delete=True, force=True, local_path=str(alias),
                           remote_path="~/work/project", dry_run=False)
    assert fleetctl.command_sync(args, None) == 0
    assert commands[0][-1] == str(safe)


def test_remote_custom_home_ancestor_is_refused(fleetctl, target):
    with pytest.raises(fleetctl.FleetError):
        fleetctl.ensure_remote_home_not_target("/scratch", "/scratch/alice", target)


@pytest.mark.parametrize("name", [".ssh", ".gnupg", ".config", ".local", ".cache"])
def test_remote_guard_rejects_protected_home_dirs_and_descendants(
    fleetctl, target, name
):
    with pytest.raises(fleetctl.FleetError, match="protected home directory"):
        fleetctl.ensure_remote_home_not_target(
            f"/scratch/alice/{name}/nested", "/scratch/alice", target
        )


def test_remote_delete_script_prints_unambiguous_real_shell_output(
    fleetctl, tmp_path
):
    import os

    home = tmp_path / "custom home"
    home.mkdir()
    destination = tmp_path / "destination alias"
    destination.symlink_to(home, target_is_directory=True)
    workdir = tmp_path / "work dir"
    workdir.mkdir()
    script = fleetctl.remote_delete_path_script(str(destination), str(workdir))
    completed = fleetctl.subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        check=True,
    )
    assert completed.stdout.splitlines() == [str(home), str(home), str(workdir)]
    assert fleetctl.parse_remote_delete_paths(
        completed, "test", str(destination), str(workdir)
    ) == (str(home), str(home), str(workdir))


def test_remote_delete_script_rejects_symlinked_home_config_root(
    fleetctl, tmp_path
):
    import os

    home = tmp_path / "remote-home"
    outside = tmp_path / "ssh-data"
    home.mkdir()
    (outside / "nested").mkdir(parents=True)
    (home / ".ssh").symlink_to(outside, target_is_directory=True)
    destination = home / ".ssh" / "nested"
    script = fleetctl.remote_delete_path_script(str(destination))
    completed = fleetctl.subprocess.run(
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
    )
    assert completed.returncode == 73
    with pytest.raises(fleetctl.FleetError, match="protected home directory"):
        fleetctl.parse_remote_delete_paths(
            completed, "test", str(destination)
        )
