"""Generic (site-agnostic) Slurm semantics, run through the full
preflight_script() pipeline against the 12 .sbatch fixtures ported from the
standalone kiac_slurm suite.

These rules hold on any Slurm cluster, so the fixtures and most assertions
carry over unchanged from the old suite's test_generic_rules.py.  What
changed mechanically:

  * `check(fixture(...))` (the old suite's `run_check(..., site=SITE)`
    wrapper) becomes `fleetctl.preflight_script(path, protocol=kiac_protocol)`.
  * Every fixture now needs an in-process ProtocolRecord/QueueRecord instead
    of a YAML SiteConfig -- see `kiac_protocol` in this file, built to cover
    the same partitions (long/short/medium/a100/h200) the fixtures reference,
    deliberately leaving "general" (used by manual_basic.sbatch) undeclared
    so it is still rejected offline.
  * `Report.exit_code()` changed meaning: 0 clean / 1 warnings / 2 error
    (fleetctl's own convention), rather than the old suite's 0 clean-or-warn
    / 1 error.  Every exit_code() assertion below is updated for that; where
    a fixture's only findings are warnings (e.g. a missing `logs/` output
    directory), the new exit code is 1, not 0.
"""

from __future__ import annotations

import pytest

from _helpers import fixture


def ids(rep) -> list[str]:
    return rep.rule_ids()


@pytest.fixture()
def kiac_protocol(fleetctl):
    """A KIAC-like protocol covering exactly the partitions the fixtures use.

    "general" (manual_basic.sbatch) is deliberately left undeclared: the
    point of that fixture is that an undocumented partition from a manual's
    own example must not pass offline.
    """
    queues = (
        fleetctl.QueueRecord(
            name="long", partition="long", max_time="48:00:00", evidence="documented",
        ),
        fleetctl.QueueRecord(
            name="short", partition="short", max_time="12:00:00",
            evidence="document-conflict", as_of="2026-09-14",
        ),
        fleetctl.QueueRecord(
            name="medium", partition="medium", max_time="24:00:00", evidence="documented",
        ),
        fleetctl.QueueRecord(
            name="a100", partition="a100", max_time="24:00:00", evidence="documented",
            gres_types=("a100",),
        ),
        fleetctl.QueueRecord(
            name="h200", partition="h200", max_time="24:00:00", evidence="verified-by-run",
            as_of="2026-09-14", account_required=True, allowed_accounts=("chiru",),
            required_qos="h200_qos", gres_types=("h200",),
        ),
    )
    return fleetctl.ProtocolRecord(
        name="kiac", kind="slurm", rule_prefix="KIAC", queues=queues,
        preferred_storage="/storage",
        storage_caveat="writability not assumed -- check on the cluster",
    )


def check(fleetctl, path, protocol, **kwargs):
    kwargs.setdefault("shellcheck", False)
    return fleetctl.preflight_script(path, protocol=protocol, **kwargs)


def test_manual_example_is_not_blessed(fleetctl, kiac_protocol):
    """The manual's own example must fail: 16GB unit and an undocumented
    'general' partition."""
    rep = check(fleetctl, fixture("manual_basic.sbatch"), kiac_protocol)
    assert "SLURM021" in ids(rep)
    assert "KIAC011" in ids(rep)
    assert rep.exit_code() == 2
    mem = [d for d in rep.items if d.rule_id == "SLURM021"][0]
    assert mem.suggestion == "--mem=16G"
    assert mem.excerpt and "--mem=16GB" in mem.excerpt


def test_valid_gpu_script_passes_offline(fleetctl, kiac_protocol):
    rep = check(fleetctl, fixture("valid_gpu.sbatch"), kiac_protocol)
    assert "SH001" in ids(rep)
    assert "KIAC010" in ids(rep)
    assert not rep.errors
    # logs/ does not exist beside the fixture, but that is a fact about this
    # machine, not the cluster, so it is INFO and a valid script exits clean.
    assert rep.exit_code() == 0


def test_shell_syntax_error(fleetctl, kiac_protocol):
    rep = check(fleetctl, fixture("syntax_error.sbatch"), kiac_protocol)
    assert "SH001" in ids(rep)
    sh001 = [d for d in rep.items if d.rule_id == "SH001"][0]
    assert sh001.level == "ERROR"
    assert rep.exit_code() == 2


def test_directive_after_executable_line(fleetctl, kiac_protocol):
    rep = check(fleetctl, fixture("directive_after_code.sbatch"), kiac_protocol)
    assert "SLURM011" in ids(rep)
    assert rep.exit_code() == 2


def test_memory_options_conflict(fleetctl, kiac_protocol):
    rep = check(fleetctl, fixture("mem_conflict.sbatch"), kiac_protocol)
    assert "SLURM023" in ids(rep)


def test_shell_variable_in_directive(fleetctl, kiac_protocol):
    rep = check(fleetctl, fixture("var_directive.sbatch"), kiac_protocol)
    assert "SLURM010" in ids(rep)
    assert rep.exit_code() == 2


def test_invalid_array_spec(fleetctl, kiac_protocol):
    rep = check(fleetctl, fixture("bad_array.sbatch"), kiac_protocol)
    assert "SLURM050" in ids(rep)


def test_array_output_collision(fleetctl, kiac_protocol):
    rep = check(fleetctl, fixture("array_collision.sbatch"), kiac_protocol)
    assert "SLURM041" in ids(rep)
    # logs/ does not exist relative to the fixture dir; sbatch opens output
    # files at submit time, so this must be flagged even with %A/%a patterns
    fs = [d for d in rep.items if d.rule_id == "FS003"]
    assert fs and "mkdir -p logs" in fs[0].suggestion
    assert rep.exit_code() == 1  # both findings are warnings


def test_strict_escalates_warnings(fleetctl, kiac_protocol):
    rep = check(fleetctl, fixture("array_collision.sbatch"), kiac_protocol, strict=True)
    assert rep.exit_code() == 2
    escalated = [d for d in rep.items if d.rule_id == "SLURM041"]
    assert escalated and escalated[0].level == "ERROR" and escalated[0].escalated


def test_bad_directives_catch_multiple_rules(fleetctl, kiac_protocol):
    rep = check(fleetctl, fixture("bad_directives.sbatch"), kiac_protocol)
    found = set(ids(rep))
    assert {"SLURM004", "SLURM001", "SLURM020", "SLURM060", "SLURM070"} <= found


def test_unknown_percent_substitution(fleetctl):
    script = fleetctl.parse_text(
        "#!/bin/bash\n#SBATCH --output=logs/%q.out\n#SBATCH --partition=medium\n"
    )
    rep = fleetctl.Report()
    fleetctl.check_generic(script, rep)
    assert "SLURM040" in rep.rule_ids()


def test_json_report_shape(fleetctl, kiac_protocol):
    import json

    rep = check(fleetctl, fixture("manual_basic.sbatch"), kiac_protocol)
    payload = json.loads(
        fleetctl.render_preflight_json(rep, "manual_basic.sbatch")
    )
    # SLURM021 (16GB) and KIAC011 (general rejected offline) are both errors
    assert payload["summary"]["error"] >= 2
    assert payload["exit_code"] == 2
    rule_ids = {d["rule_id"] for d in payload["diagnostics"]}
    assert {"SLURM021", "KIAC011"} <= rule_ids


# ---------------------------------------------------------------------------
# Every remaining generic rule ID (section 2 of the port spec), hit directly
# against hand-built directives rather than a whole fixture file -- faster
# and each test names exactly the input that trips it.
# ---------------------------------------------------------------------------


def _generic(fleetctl, text):
    script = fleetctl.parse_text(text)
    rep = fleetctl.Report()
    fleetctl.check_generic(script, rep)
    return rep


def test_slurm001_and_slurm002(fleetctl):
    rep = _generic(fleetctl, "#!/bin/bash\n#SBATCH --bogus-option=1\n#SBATCH standalone\n")
    found = set(rep.rule_ids())
    assert {"SLURM001", "SLURM002"} <= found


def test_slurm003_tokenize_error(fleetctl):
    script = fleetctl.parse_text('#!/bin/bash\n#SBATCH --job-name="unterminated\n')
    rep = fleetctl.Report()
    fleetctl.check_structure(script, rep)
    assert "SLURM003" in rep.rule_ids()


def test_slurm012_missing_shebang(fleetctl):
    script = fleetctl.parse_text("#SBATCH --partition=medium\necho hi\n")
    rep = fleetctl.Report()
    fleetctl.check_structure(script, rep)
    assert "SLURM012" in rep.rule_ids()
    diag = [d for d in rep.items if d.rule_id == "SLURM012"][0]
    assert diag.level == "ERROR"
    assert diag.suggestion == "#!/bin/bash"


def test_slurm030_invalid_time_spec(fleetctl):
    rep = _generic(fleetctl, "#!/bin/bash\n#SBATCH --time=not-a-time\n")
    diag = [d for d in rep.items if d.rule_id == "SLURM030"][0]
    assert diag.level == "ERROR"


def test_slurm020_nodes_range_form(fleetctl):
    rep = _generic(fleetctl, "#!/bin/bash\n#SBATCH --nodes=x\n")
    assert "SLURM020" in rep.rule_ids()


def test_slurm022_invalid_memory(fleetctl):
    rep = _generic(fleetctl, "#!/bin/bash\n#SBATCH --mem=16X\n")
    assert "SLURM022" in rep.rule_ids()


def test_slurm031_inverted_node_range(fleetctl):
    rep = _generic(fleetctl, "#!/bin/bash\n#SBATCH --nodes=5-2\n")
    diag = [d for d in rep.items if d.rule_id == "SLURM031"][0]
    assert diag.level == "ERROR"
    assert diag.suggestion == "--nodes=2-5"


def test_slurm042_output_error_collide(fleetctl):
    rep = _generic(
        fleetctl, "#!/bin/bash\n#SBATCH --output=job.log\n#SBATCH --error=job.log\n"
    )
    assert "SLURM042" in rep.rule_ids()


def test_slurm071_gres_and_gpus_both_set(fleetctl):
    rep = _generic(
        fleetctl, "#!/bin/bash\n#SBATCH --gres=gpu:1\n#SBATCH --gpus=1\n"
    )
    assert "SLURM071" in rep.rule_ids()


def test_slurm080_task_geometry_mismatch(fleetctl):
    rep = _generic(
        fleetctl,
        "#!/bin/bash\n#SBATCH --ntasks=5\n#SBATCH --ntasks-per-node=2\n#SBATCH --nodes=2\n",
    )
    diag = [d for d in rep.items if d.rule_id == "SLURM080"][0]
    assert diag.level == "WARN"


def test_fs001_chdir_does_not_exist(fleetctl):
    script = fleetctl.parse_text(
        "#!/bin/bash\n#SBATCH --chdir=/definitely/not/a/real/path\necho hi\n"
    )
    rep = fleetctl.Report()
    fleetctl.check_filesystem(script, rep)
    fs1 = [d for d in rep.items if d.rule_id == "FS001"]
    # Host-relative: informational, never a refusal.
    assert fs1 and fs1[0].level == "INFO"


def test_fs002_missing_executable_path(fleetctl):
    script = fleetctl.parse_text("#!/bin/bash\n/no/such/binary --flag\n")
    rep = fleetctl.Report()
    fleetctl.check_filesystem(script, rep)
    fs2 = [d for d in rep.items if d.rule_id == "FS002" and d.level == "INFO"]
    assert fs2
