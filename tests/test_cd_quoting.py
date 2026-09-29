"""render_slurm_wrapper's `cd` line (item 5): a safe-home fix, not a new feature.

Before this fix the wrapper always did `cd {shlex.quote(cwd)}`, which quotes a
leading `~/` into inert literal text no shell ever expands -- every `~`-relative
workdir failed on the scheduler with "no such file or directory".  The fix reuses
quote_remote_path, the same helper remote_shell_command already relies on for
this exact problem, so this file mostly proves the two stay in sync rather than
re-deriving the rule from scratch.

Pure unit tests against render_slurm_wrapper directly: no connection, no
subprocess beyond the local, no-network `bash -n` syntax check.
"""

from __future__ import annotations

import subprocess

from _helpers import make_protocol, make_queue


def render(fleetctl, *, cwd: str) -> str:
    protocol = make_protocol(fleetctl)
    queue = make_queue(fleetctl, name="q", partition="q")
    return fleetctl.render_slurm_wrapper(
        protocol=protocol,
        remote_script="/scratch/tok/job.sh",
        script_args=[],
        interpreter=None,
        environment={},
        cwd=cwd,
        queue=queue,
        job_name="job",
        time_limit=None,
        gpus=None,
        cpus_per_task=None,
        mem=None,
        output_path=None,
        error_path=None,
    )


def cd_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.startswith("cd ")]
    assert len(lines) == 1, f"expected exactly one cd line, got {lines!r}"
    return lines[0]


def test_bare_tilde_expands_home(fleetctl):
    text = render(fleetctl, cwd="~")
    assert cd_line(text) == "cd $HOME"


def test_tilde_relative_path_with_a_space_still_expands_home(fleetctl):
    text = render(fleetctl, cwd="~/a b")
    line = cd_line(text)
    assert line == "cd $HOME/'a b'"


def test_absolute_path_is_unaffected(fleetctl):
    text = render(fleetctl, cwd="/abs/path")
    assert cd_line(text) == "cd /abs/path"


def test_command_substitution_is_neutralized_not_executed(fleetctl):
    """A cwd of `$(id)` must render as an inert single-quoted literal.

    quote_remote_path falls through to shlex.quote for anything that is not
    `~`/`~/...`, and shlex.quote wraps a value containing `$` in single quotes
    -- which POSIX shells never expand -- so this can never run as a command.
    """
    text = render(fleetctl, cwd="$(id)")
    line = cd_line(text)
    assert line == "cd '$(id)'"
    # Never unquoted/bare in the rendered text.
    assert "cd $(id)" not in text


def test_rendered_wrapper_with_every_cwd_shape_passes_bash_dash_n(fleetctl):
    for cwd in ("~", "~/a b", "/abs/path", "$(id)", "~/proj/$(whoami)"):
        text = render(fleetctl, cwd=cwd)
        result = subprocess.run(
            ["bash", "-n"], input=text, text=True, capture_output=True, timeout=10
        )
        assert result.returncode == 0, (cwd, result.stderr)
