"""Audit caller attribution and less-churn trimming (item 8).

`record_invocation` now stamps a `caller` field from `FLEETCTL_CALLER` (bounded
to 64 printable characters), and `append_jsonl`'s trim only rewrites the file
once it drifts past the cap by the larger of 10% or 200 lines -- not on every
single append past the cap.

All in-process, against tmp_path state directories; no subprocess, no network.
"""

from __future__ import annotations

import argparse

import pytest


# -- bounded_caller_env --------------------------------------------------------


def test_bounded_caller_env_empty_when_unset(fleetctl, monkeypatch):
    monkeypatch.delenv("FLEETCTL_CALLER", raising=False)
    assert fleetctl.bounded_caller_env() == ""


def test_bounded_caller_env_passes_through_a_short_value(fleetctl, monkeypatch):
    monkeypatch.setenv("FLEETCTL_CALLER", "fleetq-scheduler")
    assert fleetctl.bounded_caller_env() == "fleetq-scheduler"


def test_bounded_caller_env_truncates_to_64_printable_characters(fleetctl, monkeypatch):
    monkeypatch.setenv("FLEETCTL_CALLER", "x" * 200)
    result = fleetctl.bounded_caller_env()
    assert len(result) == 64
    assert result == "x" * 64


def test_bounded_caller_env_drops_control_characters(fleetctl, monkeypatch):
    # A real environment variable can never hold a NUL byte (the OS itself
    # refuses it), but other control bytes -- a stray \x01, a newline, a tab
    # someone pasted in -- are all fair game and must still be dropped.
    monkeypatch.setenv("FLEETCTL_CALLER", "fleetq\x01\n\tworker")
    result = fleetctl.bounded_caller_env()
    assert "\x01" not in result
    # isprintable() also excludes bare newlines/tabs.
    assert "\n" not in result
    assert "\t" not in result
    assert result == "fleetqworker"


# -- record_invocation writes the caller field --------------------------------


def test_record_invocation_writes_caller_field(fleetctl, tmp_path, monkeypatch):
    monkeypatch.setenv("FLEETCTL_CALLER", "fleetq")
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    args = argparse.Namespace(
        config_home=paths.config_home,
        state_home=paths.state_home,
        cache_home=paths.cache_home,
        audited=True,
        command="exec",
    )
    fleetctl.record_invocation(args, head=["exec", "--dry-run"], exit_code=0)
    records = fleetctl.read_jsonl(fleetctl.audit_log_file(paths))
    assert len(records) == 1
    assert records[0]["caller"] == "fleetq"


def test_record_invocation_caller_is_empty_string_not_missing_when_unset(
    fleetctl, tmp_path, monkeypatch
):
    monkeypatch.delenv("FLEETCTL_CALLER", raising=False)
    paths = fleetctl.build_paths(
        config_home=tmp_path / "c", state_home=tmp_path / "s", cache_home=tmp_path / "ca"
    )
    args = argparse.Namespace(
        config_home=paths.config_home,
        state_home=paths.state_home,
        cache_home=paths.cache_home,
        audited=True,
        command="exec",
    )
    fleetctl.record_invocation(args, head=[], exit_code=0)
    records = fleetctl.read_jsonl(fleetctl.audit_log_file(paths))
    assert records[0]["caller"] == ""


def test_record_invocation_never_raises_even_with_a_broken_state_home(
    fleetctl, tmp_path, monkeypatch
):
    """Nothing here may raise: this runs from main()'s `finally`."""
    monkeypatch.setenv("FLEETCTL_CALLER", "x")
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("i am a file, not a directory")
    args = argparse.Namespace(
        config_home=tmp_path / "c",
        state_home=blocked,
        cache_home=tmp_path / "ca",
        audited=True,
        command="exec",
    )
    fleetctl.record_invocation(args, head=[], exit_code=0)  # must not raise


# -- append_jsonl trim threshold ------------------------------------------------


def test_trim_slack_is_the_larger_of_10_percent_or_200(fleetctl):
    assert fleetctl._trim_slack(2000) == 200  # 10% of 2000 is also 200
    assert fleetctl._trim_slack(10000) == 1000  # 10% of 10000 exceeds 200
    assert fleetctl._trim_slack(10) == 200  # tiny keep still gets the 200 floor


def test_append_does_not_trim_until_past_keep_plus_slack(fleetctl, tmp_path):
    path = tmp_path / "audit.jsonl"
    keep = 10
    slack = fleetctl._trim_slack(keep)
    # Fill up to exactly keep + slack records: no trim should have happened yet.
    for i in range(keep + slack):
        fleetctl.append_jsonl(path, {"n": i}, keep=keep)
    assert len(fleetctl.read_jsonl(path)) == keep + slack


def test_append_trims_to_keep_once_past_the_slack(fleetctl, tmp_path):
    path = tmp_path / "audit.jsonl"
    keep = 10
    slack = fleetctl._trim_slack(keep)
    for i in range(keep + slack + 1):
        fleetctl.append_jsonl(path, {"n": i}, keep=keep)
    records = fleetctl.read_jsonl(path)
    assert len(records) == keep
    # Newest records survive the trim.
    assert records[-1]["n"] == keep + slack


def test_append_jsonl_still_caps_records_permanently(fleetctl, tmp_path):
    """Regression: the relaxed threshold must not let the file grow unbounded."""
    path = tmp_path / "audit.jsonl"
    keep = 5
    for i in range(500):
        fleetctl.append_jsonl(path, {"n": i}, keep=keep)
    records = fleetctl.read_jsonl(path)
    assert len(records) <= keep + fleetctl._trim_slack(keep)
