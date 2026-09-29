"""Static #SBATCH parser: tokenization, short->long option mapping, and the
Slurm value-parsing helpers shared by the generic and site rules.

Ported from the standalone kiac_slurm suite's test_parser.py almost
verbatim -- this code was carried into the fleetctl section nearly
unchanged, so the assertions are the same, only the import source differs.
"""

import pytest

import fleetctl


def test_shebang_and_directives():
    script = fleetctl.parse_text(
        "#!/bin/bash\n"
        "#SBATCH --job-name=a\n"
        "#SBATCH --partition=long\n"
        "\n"
        "echo hi\n",
        path="mem",
    )
    assert script.shebang == "#!/bin/bash"
    assert script.get("--job-name") == "a"
    assert script.get("--partition") == "long"
    assert script.body_lines and script.body_lines[0][1] == "echo hi"
    assert not script.ignored


def test_directive_after_code_is_ignored():
    """The load-bearing trap: sbatch stops reading directives at the first
    non-comment, non-blank line, so anything after it is silently ignored
    (flagged downstream as SLURM011, but the parser's job is just to record
    it as ignored rather than as a live directive)."""
    script = fleetctl.parse_text(
        "#!/bin/bash\n"
        "#SBATCH --mem=4G\n"
        "echo start\n"
        "#SBATCH --mem=8G\n",
    )
    assert script.get("--mem") == "4G"
    assert len(script.ignored) == 1
    assert script.ignored[0].value == "8G"


def test_comments_do_not_stop_header():
    script = fleetctl.parse_text(
        "#!/bin/bash\n"
        "# a plain comment\n"
        "#SBATCH --mem=4G\n"
        "\n"
        "true\n",
    )
    assert script.get("--mem") == "4G"
    assert not script.ignored


def test_short_options_and_space_form():
    script = fleetctl.parse_text(
        "#!/bin/bash\n"
        "#SBATCH -p long\n"
        "#SBATCH --time 01:00:00\n"
        "#SBATCH -J myjob\n",
    )
    assert script.get("--partition") == "long"
    assert script.get("--time") == "01:00:00"
    assert script.get("--job-name") == "myjob"


def test_attached_short_value():
    script = fleetctl.parse_text("#!/bin/bash\n#SBATCH -p2\n#SBATCH -Jtrain\n")
    assert script.get("--partition") == "2"
    assert script.get("--job-name") == "train"


def test_valueless_option_does_not_absorb_next_token():
    script = fleetctl.parse_text("#!/bin/bash\n#SBATCH --hold\n#SBATCH --requeue foo\n")
    assert script.get("--hold") is None
    assert script.get("--requeue") is None
    # 'foo' must survive as a bare token (flagged SLURM002), not be eaten
    assert "foo" in [d.option for d in script.directives]


def test_canonical_memory_case_insensitive():
    assert fleetctl.canonical_memory("16GB") == "16G"
    assert fleetctl.canonical_memory("16gb") == "16G"
    assert fleetctl.canonical_memory("512Mb") == "512M"
    assert fleetctl.canonical_memory("16G") is None
    assert fleetctl.canonical_memory("junk") is None


def test_duplicates_are_grouped_last_wins():
    script = fleetctl.parse_text(
        "#!/bin/bash\n"
        "#SBATCH --partition=long\n"
        "#SBATCH --partition=short\n",
    )
    assert script.get("--partition") == "short"
    assert len(script.get_all("--partition")) == 2


def test_shell_var_detection():
    assert fleetctl.has_shell_var("${MEM}")
    assert fleetctl.has_shell_var("$MEM")
    assert fleetctl.has_shell_var("$(x)")
    assert not fleetctl.has_shell_var("16G")
    assert not fleetctl.has_shell_var("logs/%x_%j.out")


def test_parse_time():
    assert fleetctl.parse_time("30") == 30 * 60
    assert fleetctl.parse_time("30:00") == 30 * 60
    assert fleetctl.parse_time("01:00:00") == 3600
    assert fleetctl.parse_time("48:00:00") == 48 * 3600
    assert fleetctl.parse_time("1-12:00:00") == 36 * 3600
    assert fleetctl.parse_time("-1") == -1
    assert fleetctl.parse_time("infinite") == -1
    assert fleetctl.parse_time("junk") is None
    assert fleetctl.parse_time("1:2:3:4") is None
    assert fleetctl.parse_time("") is None
    assert fleetctl.parse_time(None) is None


def test_fmt_time_roundtrip():
    # days>0 renders D-HH:MM:SS, matching Slurm's own secs2time_str
    assert fleetctl.fmt_time(172800) == "2-00:00:00"
    assert fleetctl.fmt_time(3600) == "01:00:00"
    assert fleetctl.fmt_time(-1) == "infinite"
    assert fleetctl.fmt_time(None) == "unknown"
    assert fleetctl.fmt_time(90000) == "1-01:00:00"


def test_parse_memory():
    assert fleetctl.parse_memory("16G") == (16 * 1024, None)
    assert fleetctl.parse_memory("16GB")[1] == "B-suffix"
    assert fleetctl.parse_memory("16B")[1] == "B-suffix"
    assert fleetctl.parse_memory("512") == (512, None)
    assert fleetctl.parse_memory("0") == (0, None)
    assert fleetctl.parse_memory("16X")[1] == "invalid"
    assert fleetctl.parse_memory("-4G")[1] == "invalid"
    assert fleetctl.parse_memory(None)[1] == "invalid"


def test_expand_hostlist():
    assert fleetctl.expand_hostlist("cn[7-9]") == ["cn7", "cn8", "cn9"]
    assert fleetctl.expand_hostlist("cn[1,4]") == ["cn1", "cn4"]
    assert fleetctl.expand_hostlist("cn10") == ["cn10"]
    assert fleetctl.expand_hostlist("") == []
    assert fleetctl.expand_hostlist("cn[01-03]") == ["cn01", "cn02", "cn03"]


def test_short_to_long_mapping_is_complete_for_common_options():
    """Regression pin: the short-option table this rewrite carried over."""
    assert fleetctl.SHORT_TO_LONG["p"] == "--partition"
    assert fleetctl.SHORT_TO_LONG["A"] == "--account"
    assert fleetctl.SHORT_TO_LONG["t"] == "--time"
    assert fleetctl.SHORT_TO_LONG["q"] == "--qos"
    assert fleetctl.SHORT_TO_LONG["G"] == "--gpus"  # sbatch: -G, --gpus
    assert fleetctl.SHORT_TO_LONG["J"] == "--job-name"


def test_directive_line_stops_at_trailing_comment():
    script = fleetctl.parse_text("#!/bin/bash\n#SBATCH --mem=4G # a trailing note\n")
    assert script.get("--mem") == "4G"


@pytest.mark.parametrize("directive", ["-G 2", "--gpus=a100:2", "--gres=gpu:2,shard:1", "-B 2:8", "--extra-node-info=2:8"])
def test_valid_gpu_and_node_info_directives_are_not_refused(fleetctl, directive):
    script = fleetctl.parse_text(f"#!/bin/bash\n#SBATCH {directive}\necho hi\n")
    rep = fleetctl.Report()
    fleetctl.check_generic(script, rep)
    assert not [d for d in rep.items if d.level == "ERROR"], directive
    assert "SLURM001" not in rep.rule_ids(), directive


def test_an_invalid_spec_inside_a_gres_list_is_still_refused(fleetctl):
    script = fleetctl.parse_text("#!/bin/bash\n#SBATCH --gres=gpu:2,gpu:0\necho hi\n")
    rep = fleetctl.Report()
    fleetctl.check_generic(script, rep)
    assert "SLURM070" in rep.rule_ids()
