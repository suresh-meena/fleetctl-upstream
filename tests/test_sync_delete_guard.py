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
