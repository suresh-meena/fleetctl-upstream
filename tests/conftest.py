"""Load bin/fleetctl (no .py extension) and expose it to the test suite.

fleetctl is a single-file, stdlib-only CLI script with no `.py` suffix, so it
cannot be imported the normal way -- a SourceFileLoader/spec has to be built
by hand.  That loaded module is registered under sys.modules["fleetctl"]
*before test collection finishes*, so any test file can simply do

    import fleetctl

and use fleetctl.preflight_script, fleetctl.parse_text, fleetctl.Report,
fleetctl.ProtocolRecord, fleetctl.QueueRecord, etc. exactly like an ordinary
top-level import.  A session-scoped `fleetctl` fixture is also provided for
tests that prefer dependency injection to a bare import; both refer to the
same module object.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FLEETCTL_PATH = REPO_ROOT / "bin" / "fleetctl"

# Isolate every test from the developer's real fleet config/state/cache.  The
# unit tests below never call FleetContext.load() (they build
# ProtocolRecord/QueueRecord in-process), and the CLI end-to-end tests always
# pass explicit --config-home/--state-home/--cache-home -- but setting these
# env vars up front, before fleetctl's module body ever runs, means any code
# path that falls back to the environment instead of an explicit flag still
# lands in a throwaway scratch directory, never ~/.config/fleet or
# ~/.local/state/fleet.
_SCRATCH = Path(tempfile.mkdtemp(prefix="fleetctl-test-"))
os.environ.setdefault("FLEET_CONFIG_HOME", str(_SCRATCH / "config"))
os.environ.setdefault("FLEET_STATE_HOME", str(_SCRATCH / "state"))
os.environ.setdefault("FLEET_CACHE_HOME", str(_SCRATCH / "cache"))


def _load_fleetctl():
    loader = importlib.machinery.SourceFileLoader("fleetctl", str(FLEETCTL_PATH))
    spec = importlib.util.spec_from_loader("fleetctl", loader)
    if spec is None:  # pragma: no cover - importlib always returns one here
        raise RuntimeError(f"could not build a module spec for {FLEETCTL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fleetctl"] = module
    loader.exec_module(module)
    return module


# Loaded once, at collection time, so `import fleetctl` in every test module
# resolves to this same object via sys.modules -- exactly like a normal
# package import would.
_FLEETCTL_MODULE = _load_fleetctl()


@pytest.fixture(scope="session")
def fleetctl():
    """The loaded fleetctl module (the same object a plain `import fleetctl` returns)."""
    return _FLEETCTL_MODULE


@pytest.fixture(scope="session")
def fleetctl_path() -> Path:
    """Filesystem path to bin/fleetctl, for the subprocess/CLI tests."""
    return FLEETCTL_PATH


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT
