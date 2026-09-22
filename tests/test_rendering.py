"""render_preflight_text / render_preflight_json determinism, the
Diagnostic.confidence provenance field, and FleetError on a missing script.

None of this existed as a separate file in the standalone kiac_slurm suite
(it had render_json in report.py, exercised only inline inside
test_misc.py's test_json_report_shape); it gets its own file here because
the new API adds a text renderer and a `confidence` field worth pinning on
its own.
"""

from __future__ import annotations

import json

import pytest

from _helpers import make_protocol, make_queue


def test_preflight_script_raises_fleeterror_on_missing_file(fleetctl, tmp_path):
    protocol = make_protocol(fleetctl)
    missing = tmp_path / "does-not-exist.sbatch"
    with pytest.raises(fleetctl.FleetError):
        fleetctl.preflight_script(missing, protocol=protocol)


def test_parse_script_raises_fleeterror_directly(fleetctl, tmp_path):
    missing = tmp_path / "nope.sbatch"
    with pytest.raises(fleetctl.FleetError, match="cannot read script"):
        fleetctl.parse_script(missing)


def _sample_report(fleetctl):
    rep = fleetctl.Report()
    rep.pass_("SH001", "bash syntax valid")
    rep.warn(
        "SLURM041", "array missing %a", line=3, excerpt="#SBATCH --output=x.out",
        suggestion="use %A_%a",
    )
    rep.error(
        "KIAC011", "partition not documented", line=2, excerpt="#SBATCH --partition=general",
        suggestion="use a documented partition", confidence="documented",
    )
    return rep


def test_text_renderer_is_deterministic(fleetctl):
    rep = _sample_report(fleetctl)
    first = fleetctl.render_preflight_text(rep, "job.sbatch")
    second = fleetctl.render_preflight_text(rep, "job.sbatch")
    assert first == second
    assert "job.sbatch" in first
    assert "2 ERROR" not in first  # sanity: only one error in the sample
    assert "1 PASS, 1 WARN, 1 ERROR" in first


def test_text_renderer_marks_strict_mode(fleetctl):
    rep = _sample_report(fleetctl)
    plain = fleetctl.render_preflight_text(rep, "job.sbatch", strict=False)
    strict = fleetctl.render_preflight_text(rep, "job.sbatch", strict=True)
    assert "offline preflight" in plain and "(strict)" not in plain.splitlines()[-1]
    assert "offline preflight (strict)" in strict


def test_text_renderer_shows_escalated_marker(fleetctl):
    rep = fleetctl.Report()
    rep.warn("SLURM041", "array missing %a")
    rep.escalate_warnings()
    text = fleetctl.render_preflight_text(rep, "job.sbatch", strict=True)
    assert "(strict)" in text
    assert "SLURM041" in text


def test_json_renderer_parses_and_carries_fields(fleetctl):
    rep = _sample_report(fleetctl)
    payload = json.loads(fleetctl.render_preflight_json(rep, "job.sbatch"))
    assert payload["file"] == "job.sbatch"
    assert payload["exit_code"] == 2
    assert payload["summary"] == {"pass": 1, "info": 0, "warn": 1, "error": 1}
    by_rule = {d["rule_id"]: d for d in payload["diagnostics"]}
    assert by_rule["KIAC011"]["level"] == "ERROR"
    assert by_rule["KIAC011"]["line"] == 2
    assert by_rule["KIAC011"]["confidence"] == "documented"
    assert by_rule["SLURM041"]["level"] == "WARN"
    assert by_rule["SLURM041"]["suggestion"] == "use %A_%a"
    assert by_rule["SH001"]["confidence"] is None


def test_json_renderer_reflects_strict_flag(fleetctl):
    rep = fleetctl.Report()
    rep.warn("SLURM041", "array missing %a")
    rep.escalate_warnings()
    payload = json.loads(fleetctl.render_preflight_json(rep, "job.sbatch", strict=True))
    assert payload["strict"] is True
    assert payload["exit_code"] == 2
    diag = payload["diagnostics"][0]
    assert diag["escalated"] is True
    assert diag["level"] == "ERROR"


# ---------------------------------------------------------------------------
# Provenance: evidence/as_of on a QueueRecord reach Diagnostic.confidence
# ---------------------------------------------------------------------------


def test_confidence_field_carries_queue_evidence(fleetctl):
    verified = make_queue(
        fleetctl, name="a100", partition="a100", max_time="24:00:00",
        evidence="verified-by-run", as_of="2026-09-14",
    )
    conflicted = make_queue(
        fleetctl, name="short", partition="short", max_time="12:00:00",
        evidence="document-conflict",
    )
    protocol = make_protocol(fleetctl, rule_prefix="KIAC", queues=(verified, conflicted))

    script_a = fleetctl.parse_text(
        "#!/bin/bash\n#SBATCH --partition=a100\n#SBATCH --time=48:00:00\n"
    )
    rep_a = fleetctl.Report()
    fleetctl.check_site_policy(script_a, rep_a, protocol)
    verified_diag = [d for d in rep_a.items if d.rule_id == "KIAC033"][0]
    assert verified_diag.confidence == "verified-by-run"
    assert verified_diag.level == "ERROR"

    script_b = fleetctl.parse_text(
        "#!/bin/bash\n#SBATCH --partition=short\n#SBATCH --time=18:00:00\n"
    )
    rep_b = fleetctl.Report()
    fleetctl.check_site_policy(script_b, rep_b, protocol)
    conflicted_diag = [d for d in rep_b.items if d.rule_id == "KIAC031"][0]
    assert conflicted_diag.confidence == "document-conflict"
    # Softer finding: a document-conflict queue only ever warns here, never
    # errors, in contrast to the verified-by-run queue above which does.
    assert conflicted_diag.level == "WARN"


def test_inferred_confidence_on_unresolvable_command(fleetctl):
    """FS002's 'command not found on this host' path is explicitly tagged
    `inferred` -- it might be a real module-provided binary on the cluster,
    so the tool should not claim more certainty than it has."""
    script = fleetctl.parse_text(
        "#!/bin/bash\n#SBATCH --partition=medium\n__totally_made_up_binary__ --flag\n"
    )
    rep = fleetctl.Report()
    fleetctl.check_filesystem(script, rep)
    diag = [d for d in rep.items if d.rule_id == "FS002"][0]
    assert diag.level == "INFO"
    assert diag.confidence == fleetctl.CONF_INFERRED
