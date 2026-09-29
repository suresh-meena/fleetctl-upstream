"""Atomic writes and locks for shared mutable state (item 9).

routes.json (record_route_state), known_hosts's first creation
(write_text_if_missing) and the audit/ledger trim (append_jsonl) all do a
read-modify-write under `flock_guard` now, with an atomic tmp+rename (or
tmp+fsync+rename) replace.  These tests hammer each with real concurrency --
both threads (proves there is no silly ordering bug) and separate
`multiprocessing.Process`es (the one that actually exercises `fcntl.flock`
across processes, since threads alone are already serialized by the GIL around
the pure-Python read-modify-write) -- and assert no lost updates and no
exception.

Nothing here touches the network or spawns ssh/scp/rsync: every worker is a
local thread or a local Python subprocess calling library functions directly
against a tmp_path state directory.
"""

from __future__ import annotations

import multiprocessing
import threading

import pytest


# -- routes.json: concurrent record_route_state never loses an update --------


def test_concurrent_threads_recording_distinct_targets_lose_nothing(fleetctl, tmp_path):
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    n = 40
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        fleetctl.record_route_state(paths, f"target-{i}", "direct", f"/tmp/sock-{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    state = fleetctl.read_route_state(paths)
    assert len(state) == n, f"expected {n} recorded targets, got {len(state)}"
    for i in range(n):
        assert state[f"target-{i}"]["route"] == "direct"


def _record_one_route(state_home: str, name: str) -> None:
    """Top-level (picklable) worker for a separate OS process."""
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
    module.record_route_state(paths, name, "direct", f"/tmp/sock-{name}")


def test_concurrent_processes_recording_distinct_targets_lose_nothing(tmp_path, fleetctl):
    """The variant that actually exercises fcntl.flock across processes: two
    threads in one interpreter are already serialized by the GIL around the
    pure-Python read-modify-write, so only separate processes prove the lock
    (rather than incidental ordering) is what prevents the lost update."""
    state_home = tmp_path / "state"
    n = 8
    procs = [
        multiprocessing.Process(target=_record_one_route, args=(str(state_home), f"p{i}"))
        for i in range(n)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=state_home, cache_home=tmp_path / "ca"
    )
    state = fleetctl.read_route_state(paths)
    assert len(state) == n
    for i in range(n):
        assert f"p{i}" in state


# -- known_hosts first creation: no "refusing to overwrite" race -------------


def test_concurrent_write_text_if_missing_never_raises(fleetctl, tmp_path):
    path = tmp_path / "known_hosts"
    n = 40
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker() -> None:
        barrier.wait()
        try:
            fleetctl.write_text_if_missing(path, "", mode=fleetctl.PRIVATE_FILE_MODE)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert path.exists()
    assert path.read_text() == ""


def test_write_text_if_missing_does_not_clobber_existing_content(fleetctl, tmp_path):
    path = tmp_path / "known_hosts"
    path.write_text("existing-host-key\n")
    fleetctl.write_text_if_missing(path, "", mode=fleetctl.PRIVATE_FILE_MODE)
    assert path.read_text() == "existing-host-key\n"


# -- append_jsonl: concurrent appends racing a trim lose nothing --------------


def test_concurrent_appends_across_the_trim_threshold_lose_nothing(fleetctl, tmp_path):
    path = tmp_path / "audit.jsonl"
    keep = 20
    n = 60  # comfortably past keep + _trim_slack(keep), so a trim must fire
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        barrier.wait()
        try:
            fleetctl.append_jsonl(path, {"n": i}, keep=keep)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    records = fleetctl.read_jsonl(path)
    # Every appended record's "n" is unique; the file may have been trimmed,
    # but nothing still present should be duplicated or corrupted.
    seen = [r["n"] for r in records]
    assert len(seen) == len(set(seen)), "a record was duplicated or corrupted"
    assert len(records) <= n


def _append_one(path_str: str, n: int, keep: int) -> None:
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
    module.append_jsonl(Path(path_str), {"n": n}, keep=keep)


def test_concurrent_process_appends_lose_nothing(fleetctl, tmp_path):
    path = tmp_path / "audit.jsonl"
    keep = 10
    n = 20
    procs = [
        multiprocessing.Process(target=_append_one, args=(str(path), i, keep))
        for i in range(n)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0

    records = fleetctl.read_jsonl(path)
    seen = [r["n"] for r in records]
    assert len(seen) == len(set(seen)), "a record was duplicated or corrupted"
    assert len(records) == n  # n is well under keep + slack, so nothing trims yet
