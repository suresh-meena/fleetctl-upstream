# fleetctl test suite

Run from the `fleet-dotfiles` checkout root:

```
cd shared/fleet-dotfiles && python3 -m pytest tests -q
```

Lint `fleetctl` and its tests for unused code and likely bugs with the rules
in `bin/ruff.toml`:

```
ruff check bin/fleetctl tests
```

`bin/fleetctl` is a single-file, stdlib-only CLI with **no `.py` extension**,
so it cannot be `import`ed the normal way. `tests/conftest.py` loads it by
hand with `importlib.machinery.SourceFileLoader` and registers the result as
`sys.modules["fleetctl"]`, so every test file just does `import fleetctl` and
uses `fleetctl.preflight_script`, `fleetctl.parse_text`, `fleetctl.Report`,
`fleetctl.ProtocolRecord`, `fleetctl.QueueRecord`, etc. as if it were an
ordinary top-level module. A session-scoped `fleetctl` fixture (same module
object) is also available for tests that prefer dependency injection.

The suite is stdlib + pytest only: no network, no SSH, and nothing reads or
writes the developer's real `~/.config/fleet` or `~/.local/state/fleet` (unit
tests build `ProtocolRecord`/`QueueRecord` in-process; the CLI end-to-end
tests in `test_cli_e2e.py` always pass `--config-home`/`--state-home`/
`--cache-home` pointed at a `tmp_path`). Shell-syntax and module-resolution
checks are driven through a `FakeRunner` stub (`tests/_helpers.py`), so
nothing here depends on the developer's machine having `shellcheck` or
environment-modules installed.
