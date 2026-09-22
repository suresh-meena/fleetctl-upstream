"""Miscellaneous coverage: shell-syntax/shellcheck (SH*), module resolution
(MOD*), filesystem checks (FS*) driven by a fake command runner, and the
Report container's own bookkeeping (counts/worst_level/escalate_warnings).

Ported from the standalone kiac_slurm suite's test_misc.py.  Everything that
depended on the old `bin/kiac-slurm`/`bin/slurm-check` wrapper scripts or the
YAML SiteConfig (`SITE.assume_storage_writable`) does not apply here and is
dropped; a `generator.py`-style template generator was never part of this
section either.  Every command runner here is a FakeRunner (tests/_helpers.py)
so nothing depends on the developer's machine having shellcheck or the
`module` command installed -- read _helpers.py for the stub protocol, which
matches the `_CommandRunner.run(cmd) -> (rc, out)` shape fleetctl expects.
"""

from __future__ import annotations

from _helpers import FakeRunner


# ---------------------------------------------------------------------------
# SH001 / SH100 / SH101 -- shell syntax and shellcheck, entirely stubbed
# ---------------------------------------------------------------------------


def test_sh001_bash_missing_is_info_not_error(fleetctl, tmp_path):
    script = tmp_path / "j.sbatch"
    script.write_text("#!/bin/bash\necho hi\n")
    rep = fleetctl.Report()
    runner = FakeRunner({("bash", "-n"): (127, "command not found: bash")})
    fleetctl.check_shell(str(script), rep, runner=runner, shellcheck="off")
    diag = [d for d in rep.items if d.rule_id == "SH001"][0]
    assert diag.level == "INFO"


def test_sh001_syntax_error_is_error(fleetctl, tmp_path):
    script = tmp_path / "j.sbatch"
    script.write_text("#!/bin/bash\nif [ -f x ]; then\n")
    rep = fleetctl.Report()
    runner = FakeRunner({("bash", "-n"): (2, "line 2: unexpected EOF")})
    fleetctl.check_shell(str(script), rep, runner=runner, shellcheck="off")
    diag = [d for d in rep.items if d.rule_id == "SH001"][0]
    assert diag.level == "ERROR"
    assert diag.excerpt and "unexpected EOF" in diag.excerpt


def test_sh100_shellcheck_requested_but_absent(fleetctl, tmp_path):
    script = tmp_path / "j.sbatch"
    script.write_text("#!/bin/bash\necho hi\n")
    rep = fleetctl.Report()
    runner = FakeRunner(
        {
            ("bash", "-n"): (0, ""),
            ("shellcheck", "--version"): (127, "command not found: shellcheck"),
        }
    )
    fleetctl.check_shell(str(script), rep, runner=runner, shellcheck="on")
    diag = [d for d in rep.items if d.rule_id == "SH100"][0]
    assert diag.level == "WARN"


def test_shellcheck_off_never_probes_for_it(fleetctl, tmp_path):
    """shellcheck='off' (preflight_script's default) must not even ask
    whether shellcheck exists -- that's the whole point of the default."""
    script = tmp_path / "j.sbatch"
    script.write_text("#!/bin/bash\necho hi\n")
    rep = fleetctl.Report()
    runner = FakeRunner({("bash", "-n"): (0, "")})
    fleetctl.check_shell(str(script), rep, runner=runner, shellcheck="off")
    assert not any(call[:1] == ["shellcheck"] for call in runner.calls)
    assert "SH100" not in rep.rule_ids()
    assert "SH101" not in rep.rule_ids()


def test_sh101_clean_and_dirty(fleetctl, tmp_path):
    script = tmp_path / "j.sbatch"
    script.write_text("#!/bin/bash\necho hi\n")

    clean_runner = FakeRunner(
        {
            ("bash", "-n"): (0, ""),
            ("shellcheck", "--version"): (0, "8.0.0"),
            ("shellcheck", "-s", "bash"): (0, ""),
        }
    )
    rep = fleetctl.Report()
    fleetctl.check_shell(str(script), rep, runner=clean_runner, shellcheck="on")
    assert [d for d in rep.items if d.rule_id == "SH101"][0].level == "PASS"

    dirty_runner = FakeRunner(
        {
            ("bash", "-n"): (0, ""),
            ("shellcheck", "--version"): (0, "8.0.0"),
            ("shellcheck", "-s", "bash"): (1, "SC2086: quote this"),
        }
    )
    rep2 = fleetctl.Report()
    fleetctl.check_shell(str(script), rep2, runner=dirty_runner, shellcheck="on")
    warn = [d for d in rep2.items if d.rule_id == "SH101"][0]
    assert warn.level == "WARN"
    assert warn.excerpt and "SC2086" in warn.excerpt


# ---------------------------------------------------------------------------
# MOD001 / MOD002 -- module resolution, entirely stubbed
# ---------------------------------------------------------------------------


def test_module_check_resolves_and_flags(fleetctl):
    script = fleetctl.parse_text(
        "#!/bin/bash\n#SBATCH --partition=a100\nmodule load python/3.11 cuda/12.1\n"
    )

    class ModRunner(FakeRunner):
        def run(self, cmd):
            super().run(cmd)
            if cmd[:2] == ["bash", "-lc"] and "module show" in cmd[2]:
                name = cmd[2].split(">/dev/null")[0].split()[-1].strip("'\"")
                return (0, "") if name == "python/3.11" else (1, "error")
            if cmd[:3] == ["bash", "-lc", "type module"]:
                return 0, ""
            return 0, ""

    rep = fleetctl.Report()
    fleetctl.check_modules(script, rep, ModRunner())
    levels = [d.level for d in rep.items if d.rule_id == "MOD002"]
    assert levels == ["PASS", "WARN"]  # python resolves, cuda flags


def test_module_check_skips_when_modulecmd_absent(fleetctl):
    script = fleetctl.parse_text(
        "#!/bin/bash\n#SBATCH --partition=a100\nmodule load python/3.11\n"
    )

    class NoModRunner(FakeRunner):
        def run(self, cmd):
            super().run(cmd)
            if cmd[:3] == ["bash", "-lc", "type module"]:
                return 1, "not found"
            return 0, ""

    rep = fleetctl.Report()
    fleetctl.check_modules(script, rep, NoModRunner())
    assert rep.rule_ids() == ["MOD001"]


def test_module_check_no_loads_is_silent(fleetctl):
    script = fleetctl.parse_text("#!/bin/bash\n#SBATCH --partition=a100\necho hi\n")
    rep = fleetctl.Report()
    fleetctl.check_modules(script, rep, FakeRunner())
    assert rep.rule_ids() == []


# ---------------------------------------------------------------------------
# FS001 / FS002 / FS003 -- filesystem references, against the real filesystem
# (these deliberately touch a real tmp_path/cwd, same as the old suite did;
# no command runner involved)
# ---------------------------------------------------------------------------


def test_fs_invalid_workdir(fleetctl):
    script = fleetctl.parse_text(
        "#!/bin/bash\n"
        "#SBATCH --partition=medium\n"
        "cd /definitely/not/a/real/path\n"
        "python3 task.py\n"
    )
    rep = fleetctl.Report()
    fleetctl.check_filesystem(script, rep)
    fs1 = [d for d in rep.items if d.rule_id == "FS001"]
    assert fs1 and fs1[0].level == "INFO"
    assert fs1[0].line is not None


def test_fs003_output_dir_missing(fleetctl, tmp_path):
    script_path = tmp_path / "job.sbatch"
    script_path.write_text(
        "#!/bin/bash\n#SBATCH --output=out/%x_%j.log\n#SBATCH --partition=medium\necho hi\n"
    )
    script = fleetctl.parse_script(script_path)
    rep = fleetctl.Report()
    fleetctl.check_filesystem(script, rep)
    fs3 = [d for d in rep.items if d.rule_id == "FS003"]
    assert fs3 and fs3[0].level == "INFO"
    (tmp_path / "out").mkdir()
    rep2 = fleetctl.Report()
    fleetctl.check_filesystem(script, rep2)
    assert not [d for d in rep2.items if d.rule_id == "FS003"]


# ---------------------------------------------------------------------------
# _CommandRunner -- the real subprocess facade, exercised for real (no
# stubbing here on purpose: this is the one place we *want* to prove a
# missing binary becomes a diagnostic rather than a crash).
# ---------------------------------------------------------------------------


def test_command_runner_missing_binary_is_127_not_a_crash(fleetctl):
    runner = fleetctl._CommandRunner(timeout=5)
    rc, out = runner.run(["__no_such_command_xyz__"])
    assert rc == 127
    assert "command not found" in out


# ---------------------------------------------------------------------------
# Report bookkeeping: counts / worst_level / escalate_warnings / exit_code
# ---------------------------------------------------------------------------


def test_report_worst_level_and_counts(fleetctl):
    rep = fleetctl.Report()
    assert rep.worst_level() == "PASS"  # empty report reads as clean
    rep.pass_("X001", "ok")
    rep.info("X002", "fyi")
    rep.warn("X003", "careful")
    assert rep.worst_level() == "WARN"
    rep.error("X004", "no")
    assert rep.worst_level() == "ERROR"
    assert rep.counts() == {"pass": 1, "info": 1, "warn": 1, "error": 1}
    assert rep.exit_code() == 2


def test_escalate_warnings_only_touches_warn(fleetctl):
    rep = fleetctl.Report()
    rep.pass_("X001", "ok")
    rep.warn("X002", "careful")
    rep.error("X003", "no")
    count = rep.escalate_warnings()
    assert count == 1
    assert rep.counts() == {"pass": 1, "info": 0, "warn": 0, "error": 2}
    escalated = [d for d in rep.items if d.rule_id == "X002"][0]
    assert escalated.level == "ERROR"
    assert escalated.escalated is True
    untouched_pass = [d for d in rep.items if d.rule_id == "X001"][0]
    assert untouched_pass.escalated is False
    original_error = [d for d in rep.items if d.rule_id == "X003"][0]
    assert original_error.escalated is False


def test_exit_code_truth_table(fleetctl):
    clean = fleetctl.Report()
    clean.pass_("X001", "ok")
    assert clean.exit_code() == 0

    warned = fleetctl.Report()
    warned.warn("X001", "careful")
    assert warned.exit_code() == 1

    errored = fleetctl.Report()
    errored.warn("X001", "careful")
    errored.error("X002", "no")
    assert errored.exit_code() == 2
