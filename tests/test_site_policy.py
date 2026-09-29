"""Site policy (stage 5), rewritten from scratch against in-process
ProtocolRecord/QueueRecord fixtures.

This replaces the standalone kiac_slurm suite's test_kiac_rules.py and
test_sites.py, which drove policy from a hand-written per-cluster YAML
SiteConfig (config/kiac.yaml, config/amd.yaml) loaded through
`load_site_config()`.  That YAML layer is gone: the same shape of policy --
which partitions exist, their account/QOS matrix, GRES types, MaxTime,
GPU-only status, storage recommendation -- is now just fields on
ProtocolRecord/QueueRecord that a fleet config TOML file populates.  So
every test here builds its own minimal protocol/queue objects in Python
instead of reading a YAML fixture, using `check_site_policy` directly (the
same style the old suite used for `check_kiac`) where a whole
preflight_script() run would be more setup than the assertion needs.

Rule IDs are always `f"{protocol.rule_prefix}{code}"` -- most fixtures below
use rule_prefix="KIAC" to keep the numbers directly comparable to the old
suite's KIAC02x expectations, but test_custom_rule_prefix_is_honored proves
the prefix is not hardcoded anywhere in the engine.
"""

from __future__ import annotations

import pytest

from _helpers import make_protocol, make_queue


def ids(rep) -> list[str]:
    return rep.rule_ids()


def run_site_policy(fleetctl, protocol, text):
    script = fleetctl.parse_text(text)
    rep = fleetctl.Report()
    fleetctl.check_site_policy(script, rep, protocol)
    return rep, script


# ---------------------------------------------------------------------------
# Partition resolution: missing / unknown / documented / example-only
# ---------------------------------------------------------------------------


def test_missing_partition_is_an_error(fleetctl):
    protocol = make_protocol(fleetctl, rule_prefix="KIAC")
    rep, _ = run_site_policy(fleetctl, protocol, "#!/bin/bash\n#SBATCH --mem=4G\n")
    assert "KIAC001" in ids(rep)
    assert rep.errors[0].rule_id == "KIAC001"


def test_unknown_partition_rejected_offline(fleetctl):
    """No live scheduler exists at this stage to ask, so a partition named
    only in an example script -- exactly what the KIAC manual's own basic
    example does with 'general' -- must not pass offline."""
    protocol = make_protocol(
        fleetctl,
        rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="long", partition="long"),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=general\n"
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC011"][0]
    assert diag.level == "ERROR"
    assert "general" in diag.message
    assert "long" in diag.message  # known partitions are listed in the detail
    assert diag.confidence == "documented"


def test_documented_partition_passes(fleetctl):
    protocol = make_protocol(
        fleetctl,
        rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="a100", partition="a100", evidence="documented"),),
    )
    rep, _ = run_site_policy(fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=a100\n")
    diag = [d for d in rep.items if d.rule_id == "KIAC010"][0]
    assert diag.level == "PASS"
    assert diag.confidence == "documented"


def test_example_only_partition_gets_info_not_silence(fleetctl):
    protocol = make_protocol(
        fleetctl,
        rule_prefix="AMD",
        queues=(
            make_queue(
                fleetctl, name="GPU", partition="GPU", notes=("example-only",),
            ),
        ),
    )
    rep, _ = run_site_policy(fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=GPU\n")
    a10 = [d for d in rep.items if d.rule_id == "AMD010"]
    assert any(d.level == "PASS" for d in a10)
    assert any(d.level == "INFO" and "example" in d.message.lower() for d in a10)


def test_custom_rule_prefix_is_honored(fleetctl):
    protocol = make_protocol(fleetctl, rule_prefix="AMD")
    rep, _ = run_site_policy(fleetctl, protocol, "#!/bin/bash\n#SBATCH --mem=4G\n")
    assert "AMD001" in ids(rep)
    assert "KIAC001" not in ids(rep)


def test_missing_rule_prefix_falls_back_to_site(fleetctl):
    protocol = make_protocol(fleetctl, rule_prefix=None)
    rep, _ = run_site_policy(fleetctl, protocol, "#!/bin/bash\n#SBATCH --mem=4G\n")
    assert "SITE001" in ids(rep)


# ---------------------------------------------------------------------------
# Account policy: the a100/chiru trap.  sbatch --test-only does not enforce
# account-partition policy, so a request from a denied account passes the
# dry run cleanly and then pends forever -- this is why the matrix is
# checked from declared data instead of being left for a dry run to catch.
# ---------------------------------------------------------------------------


def _a100_h200_protocol(fleetctl):
    a100 = make_queue(
        fleetctl,
        name="a100",
        partition="a100",
        max_time="24:00:00",
        evidence="verified-by-run",
        as_of="2026-09-14",
        allowed_accounts=("research",),
        denied_accounts=("chiru",),
        gres_types=("a100",),
    )
    h200 = make_queue(
        fleetctl,
        name="h200",
        partition="h200",
        max_time="24:00:00",
        evidence="verified-by-run",
        as_of="2026-09-14",
        account_required=True,
        allowed_accounts=("chiru",),
        required_qos="h200_qos",
    )
    return make_protocol(fleetctl, rule_prefix="KIAC", queues=(a100, h200))


def test_account_denied_on_partition(fleetctl):
    protocol = _a100_h200_protocol(fleetctl)
    rep, _ = run_site_policy(
        fleetctl, protocol,
        "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --account=chiru\n",
    )
    k23 = [d for d in rep.items if d.rule_id == "KIAC023"][0]
    assert k23.level == "ERROR"
    assert "pending" in k23.message.lower() or "test-only" in k23.message.lower()
    assert k23.suggestion == "use one of: research"
    assert k23.confidence == "verified-by-run"


def test_account_allowed_on_partition(fleetctl):
    protocol = _a100_h200_protocol(fleetctl)
    rep, _ = run_site_policy(
        fleetctl, protocol,
        "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --account=research\n",
    )
    k23 = [d for d in rep.items if d.rule_id == "KIAC023"][0]
    assert k23.level == "PASS"


def test_account_policy_silent_without_an_account_directive(fleetctl):
    """--test-only's blind spot is about accounts, not about a script that
    never names one at all -- no account means nothing to check yet."""
    protocol = _a100_h200_protocol(fleetctl)
    rep, _ = run_site_policy(fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=a100\n")
    assert "KIAC023" not in ids(rep)


def test_test_only_blind_spot_caveat_fires_when_policy_is_declared(fleetctl):
    protocol = _a100_h200_protocol(fleetctl)
    rep, _ = run_site_policy(fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=a100\n")
    caveat = [d for d in rep.items if d.rule_id == "LIVE002"]
    assert caveat and caveat[0].level == "INFO"
    assert "does not enforce" in caveat[0].message or "not enforce" in caveat[0].message


def test_test_only_blind_spot_caveat_silent_without_policy(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="cpu", partition="cpu"),),
    )
    rep, _ = run_site_policy(fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=cpu\n")
    assert "LIVE002" not in ids(rep)


# ---------------------------------------------------------------------------
# QOS: required present / absent / wrong
# ---------------------------------------------------------------------------


def test_qos_required_but_absent(fleetctl):
    protocol = _a100_h200_protocol(fleetctl)
    rep, _ = run_site_policy(
        fleetctl, protocol,
        "#!/bin/bash\n#SBATCH --partition=h200\n#SBATCH --account=chiru\n",
    )
    k24 = [d for d in rep.items if d.rule_id == "KIAC024"][0]
    assert k24.level == "ERROR"
    assert k24.suggestion == "Add: #SBATCH --qos=h200_qos"


def test_qos_required_and_wrong(fleetctl):
    protocol = _a100_h200_protocol(fleetctl)
    rep, _ = run_site_policy(
        fleetctl, protocol,
        "#!/bin/bash\n#SBATCH --partition=h200\n#SBATCH --account=chiru\n#SBATCH --qos=normal\n",
    )
    k24 = [d for d in rep.items if d.rule_id == "KIAC024"][0]
    assert k24.level == "ERROR"
    assert "h200_qos" in k24.message


def test_qos_required_and_correct(fleetctl):
    protocol = _a100_h200_protocol(fleetctl)
    rep, _ = run_site_policy(
        fleetctl, protocol,
        "#!/bin/bash\n#SBATCH --partition=h200\n#SBATCH --account=chiru\n"
        "#SBATCH --qos=h200_qos\n",
    )
    assert not rep.errors
    k24 = [d for d in rep.items if d.rule_id == "KIAC024"][0]
    assert k24.level == "PASS"


def test_missing_account_on_h200(fleetctl):
    protocol = _a100_h200_protocol(fleetctl)
    rep, _ = run_site_policy(fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=h200\n")
    k20 = [d for d in rep.items if d.rule_id == "KIAC020"][0]
    assert k20.level == "ERROR"
    assert "H200 jobs require an account." in k20.message
    assert k20.suggestion and "--account=" in k20.suggestion


# ---------------------------------------------------------------------------
# Time vs max_time, across the three evidence tiers
# ---------------------------------------------------------------------------


def test_time_within_documented_max(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="long", partition="long", max_time="48:00:00",
                            evidence="documented"),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=long\n#SBATCH --time=10:00:00\n"
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC030"][0]
    assert diag.level == "PASS"


def test_time_exceeds_documented_max(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="long", partition="long", max_time="48:00:00",
                            evidence="documented"),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=long\n#SBATCH --time=72:00:00\n"
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC030"][0]
    assert diag.level == "ERROR"
    assert diag.confidence == "documented"


def test_time_disputed_window_warns_not_errors(fleetctl):
    """A document-conflict queue is a softer finding than a plain documented
    one -- WARN (031), not ERROR, even though the request exceeds the one
    candidate MaxTime available to compare against."""
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="short", partition="short", max_time="12:00:00",
                            evidence="document-conflict", as_of="2026-09-14"),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=short\n#SBATCH --time=18:00:00\n"
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC031"][0]
    assert diag.level == "WARN"
    assert diag.confidence == "document-conflict"
    assert rep.exit_code() == 1  # warning only, no error


def test_time_disputed_window_fits_is_info(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="short", partition="short", max_time="12:00:00",
                            evidence="document-conflict"),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=short\n#SBATCH --time=06:00:00\n"
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC032"][0]
    assert diag.level == "INFO"


def test_time_exceeds_verified_max_is_hard_error(fleetctl):
    """A verified-by-run/verified-live queue is the strictest tier: ERROR
    (033), not a WARN, distinguishing it from the document-conflict case
    above even though both compare a single request against a single number."""
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="a100", partition="a100", max_time="24:00:00",
                            evidence="verified-live", as_of="2026-09-14"),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --time=30:00:00\n"
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC033"][0]
    assert diag.level == "ERROR"
    assert diag.confidence == "verified-live"
    # fmt_time renders whole days as D-HH:MM:SS, so 24h reads as "1-00:00:00"
    assert "1-00:00:00" in diag.message


def test_time_check_silent_without_max_time_declared(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="cpu", partition="cpu"),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=cpu\n#SBATCH --time=999:00:00\n"
    )
    assert not [d for d in rep.items if d.rule_id.endswith(("030", "031", "032", "033"))]


# ---------------------------------------------------------------------------
# GRES types: documented vs verified, match vs mismatch
# ---------------------------------------------------------------------------


def test_gres_type_matches_documented_catalog(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="medium", partition="medium",
                            gres_types=("a5000", "a6000", "ada6000"), evidence="documented"),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=medium\n#SBATCH --gres=gpu:ada6000:1\n"
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC050"][0]
    assert diag.level == "PASS"


def test_gres_type_mismatches_documented_catalog(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="medium", partition="medium",
                            gres_types=("a5000", "a6000", "ada6000"), evidence="documented"),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=medium\n#SBATCH --gres=gpu:h200:1\n"
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC050"][0]
    assert diag.level == "WARN"


def test_gres_type_matches_verified_matrix(fleetctl):
    protocol = _a100_h200_protocol(fleetctl)
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --gres=gpu:a100:1\n"
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC051"][0]
    assert diag.level == "PASS"
    assert diag.confidence == "verified-by-run"


def test_gres_type_mismatches_verified_matrix(fleetctl):
    protocol = _a100_h200_protocol(fleetctl)
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --gres=gpu:h200:1\n"
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC051"][0]
    assert diag.level == "ERROR"


def test_gres_check_silent_when_no_type_named(fleetctl):
    """A bare --gres=gpu:2 names no type, so there is nothing to compare
    against the catalog -- the count-only case is generic rules' job."""
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="a100", partition="a100", gres_types=("a100",)),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --gres=gpu:1\n"
    )
    assert not [d for d in rep.items if d.rule_id in ("KIAC050", "KIAC051")]


# ---------------------------------------------------------------------------
# GPU-only queues rejecting CPU-only jobs
# ---------------------------------------------------------------------------


def test_gpu_only_queue_rejects_cpu_only_job(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="AMD",
        queues=(make_queue(fleetctl, name="GPU", partition="GPU", gpu_only=True),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol,
        "#!/bin/bash\n#SBATCH --partition=GPU\n#SBATCH --ntasks=1\npython3 cpu_only.py\n",
    )
    diag = [d for d in rep.items if d.rule_id == "AMD070"][0]
    assert diag.level == "ERROR"
    assert "monitored" in diag.message


def test_gpu_only_queue_accepts_gpu_job(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="AMD",
        queues=(make_queue(fleetctl, name="GPU", partition="GPU", gpu_only=True),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol,
        "#!/bin/bash\n#SBATCH --partition=GPU\n#SBATCH --gres=gpu:1\npython3 train.py\n",
    )
    assert "AMD070" not in ids(rep)


def test_non_gpu_only_queue_never_fires_070(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="medium", partition="medium", gpu_only=False),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=medium\npython3 x.py\n"
    )
    assert "KIAC070" not in ids(rep)


# ---------------------------------------------------------------------------
# GPU vendor tooling: nvidia-smi against AMD hardware fails silent, not loud
# ---------------------------------------------------------------------------


def test_amd_vendor_rejects_nvidia_smi(fleetctl):
    protocol = make_protocol(fleetctl, rule_prefix="AMD", gpu_vendor="amd")
    rep, _ = run_site_policy(
        fleetctl, protocol,
        "#!/bin/bash\n#SBATCH --partition=GPU\npwd; hostname\nnvidia-smi\n",
    )
    diag = [d for d in rep.items if d.rule_id == "AMD071"][0]
    assert diag.level == "ERROR"
    assert "rocm-smi" in diag.suggestion
    assert diag.line is not None


def test_nvidia_vendor_does_not_flag_nvidia_smi(fleetctl):
    protocol = make_protocol(fleetctl, rule_prefix="KIAC", gpu_vendor="nvidia")
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=a100\nnvidia-smi\n"
    )
    assert "KIAC071" not in ids(rep)


def test_no_gpu_vendor_declared_never_flags_nvidia_smi(fleetctl):
    protocol = make_protocol(fleetctl, rule_prefix="KIAC")
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=a100\nnvidia-smi\n"
    )
    assert "KIAC071" not in ids(rep)


# ---------------------------------------------------------------------------
# Storage preference vs home_prefix
# ---------------------------------------------------------------------------


def test_storage_on_preferred_area_passes(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC", preferred_storage="/storage", home_prefix="/home/alice",
        storage_caveat="writability not assumed",
    )
    rep, _ = run_site_policy(
        fleetctl, protocol,
        "#!/bin/bash\n#SBATCH --partition=medium\ncd /storage/alice/proj\npython3 x.py\n",
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC040"][0]
    assert diag.level == "PASS"
    assert "/storage" in diag.message
    assert "writability not assumed" in diag.message


def test_storage_under_home_warns(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC", preferred_storage="/storage", home_prefix="/home/alice",
    )
    rep, _ = run_site_policy(
        fleetctl, protocol,
        "#!/bin/bash\n#SBATCH --partition=medium\ncd /home/alice/proj\npython3 x.py\n",
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC040"][0]
    assert diag.level == "WARN"
    assert "/storage" in diag.suggestion


def test_storage_check_silent_without_preferred_storage(fleetctl):
    protocol = make_protocol(fleetctl, rule_prefix="KIAC", preferred_storage=None)
    rep, _ = run_site_policy(
        fleetctl, protocol,
        "#!/bin/bash\n#SBATCH --partition=medium\ncd /home/alice/proj\n",
    )
    assert "KIAC040" not in ids(rep)


def test_storage_check_ignores_shell_variable_targets(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC", preferred_storage="/storage", home_prefix="/home/alice",
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=medium\ncd $SCRATCH\n",
    )
    assert "KIAC040" not in ids(rep)


# ---------------------------------------------------------------------------
# Nodelist vs. the partition a queue's `nodes` hostlist documents it under
# ---------------------------------------------------------------------------


def test_nodelist_partition_mismatch(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(
            make_queue(fleetctl, name="long", partition="long"),
            make_queue(
                fleetctl, name="ada", partition="ada",
                nodes=("cn[7-9]",),
            ),
        ),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=long\n#SBATCH --nodelist=cn9\n"
    )
    diag = [d for d in rep.items if d.rule_id == "KIAC060"][0]
    assert diag.level == "WARN"
    assert "ada" in diag.message
    assert "long" in diag.message


def test_nodelist_matching_partition_is_silent(fleetctl):
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="ada", partition="ada", nodes=("cn[7-9]",)),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=ada\n#SBATCH --nodelist=cn7\n"
    )
    assert "KIAC060" not in ids(rep)


def test_nodelist_check_silent_when_no_queue_declares_nodes(fleetctl):
    """QueueRecord carries no node inventory by default; this is an opt-in
    check that is a silent no-op when nothing declares `capabilities.nodes`,
    the same as every other rule here when its data is simply absent."""
    protocol = make_protocol(
        fleetctl, rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="long", partition="long"),),
    )
    rep, _ = run_site_policy(
        fleetctl, protocol, "#!/bin/bash\n#SBATCH --partition=long\n#SBATCH --nodelist=cn9\n"
    )
    assert "KIAC060" not in ids(rep)


# ---------------------------------------------------------------------------
# kind="direct": generic rules run, site rules do not
# ---------------------------------------------------------------------------


def test_direct_protocol_runs_generic_but_not_site_rules(fleetctl, tmp_path):
    protocol = fleetctl.ProtocolRecord(name="workstation", kind="direct")
    script_path = tmp_path / "job.sh"
    # A directive-shaped comment and a bogus mem unit still trip the generic
    # rules; --partition names nothing a "direct" protocol has any queues
    # for, and must not be treated as an error the way KIAC011 would be.
    script_path.write_text(
        "#!/bin/bash\n#SBATCH --mem=16GB\n#SBATCH --partition=whatever\necho hi\n"
    )
    rep = fleetctl.preflight_script(script_path, protocol=protocol)
    assert "SLURM021" in ids(rep)  # generic rule still runs
    assert not any(d.rule_id.startswith(("KIAC", "SITE")) for d in rep.items)


def test_slurm_protocol_runs_site_rules_too(fleetctl, tmp_path):
    protocol = make_protocol(
        fleetctl, kind="slurm", rule_prefix="KIAC",
        queues=(make_queue(fleetctl, name="medium", partition="medium"),),
    )
    script_path = tmp_path / "job.sh"
    script_path.write_text("#!/bin/bash\n#SBATCH --partition=medium\necho hi\n")
    rep = fleetctl.preflight_script(script_path, protocol=protocol)
    assert "KIAC010" in ids(rep)


@pytest.mark.parametrize(
    "directive",
    ["--gres=gpu:h100:2", "--gres=gpu:h100", "--gpus=h100:2", "-G h100:2", "--gres=shard:1,gpu:h100:1"],
)
def test_unverified_gpu_type_is_refused_in_every_spelling(fleetctl, directive):
    """`gpu:<type>` with no count, `--gpus`'s `[type:]count`, `-G`, and a GRES
    list all name a type; none may skip the verified-type check."""
    text = f"#!/bin/bash\n#SBATCH -p a100\n#SBATCH {directive}\necho hi\n"
    rep, _ = run_site_policy(fleetctl, _a100_h200_protocol(fleetctl), text)
    assert "KIAC051" in [d.rule_id for d in rep.items if d.level == "ERROR"], directive


@pytest.mark.parametrize("directive", ["--gres=gpu:a100", "--gpus=a100:2", "-G a100:1"])
def test_verified_gpu_type_passes_in_every_spelling(fleetctl, directive):
    text = f"#!/bin/bash\n#SBATCH -p a100\n#SBATCH {directive}\necho hi\n"
    rep, _ = run_site_policy(fleetctl, _a100_h200_protocol(fleetctl), text)
    assert "KIAC051" not in [d.rule_id for d in rep.items if d.level == "ERROR"], directive
