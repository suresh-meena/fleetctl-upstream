"""Shared helpers for the fleetctl preflight test suite.

Mirrors the shape of the old kiac_slurm test suite's helpers.py/_path.py: a
fixture-path lookup and a fake command runner that fakes `bash -n`,
`shellcheck`, and `module`, so no test here depends on the developer's
machine having shellcheck or environment-modules installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture(name: str) -> Path:
    """Path to one of the copied .sbatch fixtures under tests/fixtures/."""
    return FIXTURES / name


class FakeRunner:
    """Command runner stub matching the `.run(cmd) -> (rc, out)` protocol that
    check_shell/check_modules/preflight_script expect from `_CommandRunner`.

    Responses are keyed by a command *prefix* tuple, so
    `FakeRunner({("bash", "-n"): (0, "")})` answers any `["bash", "-n", path]`
    call regardless of the trailing path argument.  Unmatched commands
    default to success with empty output, and every call is recorded so a
    test can assert on what was actually invoked (or, just as often, that
    sbatch/ssh was never invoked at all).
    """

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str]] | None = None) -> None:
        self.responses = {tuple(k): v for k, v in (responses or {}).items()}
        self.calls: list[list[str]] = []

    def run(self, cmd: list[str]) -> tuple[int, str]:
        self.calls.append(list(cmd))
        for key, value in self.responses.items():
            if tuple(cmd[: len(key)]) == key:
                return value
        return 0, ""

    def add(self, key: tuple[str, ...], rc: int, out: str) -> None:
        self.responses[tuple(key)] = (rc, out)


def make_queue(fleetctl: Any, **kwargs: Any):
    """Build a QueueRecord with sane defaults, overridden by kwargs."""
    defaults: dict[str, Any] = {"name": "q", "partition": "q"}
    defaults.update(kwargs)
    return fleetctl.QueueRecord(**defaults)


def make_protocol(fleetctl: Any, **kwargs: Any):
    """Build a slurm-kind ProtocolRecord with sane defaults, overridden by kwargs."""
    defaults: dict[str, Any] = {"name": "site", "kind": "slurm", "rule_prefix": "SITE"}
    defaults.update(kwargs)
    return fleetctl.ProtocolRecord(**defaults)
