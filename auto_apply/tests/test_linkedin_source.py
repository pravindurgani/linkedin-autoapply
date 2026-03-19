"""Tests for auto_apply/sources/linkedin.py — Phase 11.4 and 11.5."""

import pytest

from auto_apply.sources.linkedin import _canonical_url


# ── Phase 11.4 — _canonical_url() ────────────────────────────────────────────


def test_canonical_url_strips_tracking_params():
    raw = (
        "https://www.linkedin.com/jobs/view/1234567890/"
        "?refId=abc123&trackingId=xyz789&trk=pub-jobs_jac-search"
    )
    assert _canonical_url(raw) == "https://www.linkedin.com/jobs/view/1234567890/"


def test_canonical_url_strips_utm_params():
    raw = (
        "https://www.linkedin.com/jobs/view/9876543210/"
        "?utm_source=linkedin&utm_medium=email&utm_campaign=jobs"
    )
    assert _canonical_url(raw) == "https://www.linkedin.com/jobs/view/9876543210/"


def test_canonical_url_strips_fragment():
    raw = "https://www.linkedin.com/jobs/view/1111111111/#apply"
    assert _canonical_url(raw) == "https://www.linkedin.com/jobs/view/1111111111/"


def test_canonical_url_strips_query_and_fragment():
    raw = "https://www.linkedin.com/jobs/view/2222222222/?ref=foo#section"
    assert _canonical_url(raw) == "https://www.linkedin.com/jobs/view/2222222222/"


def test_canonical_url_clean_url_unchanged():
    clean = "https://www.linkedin.com/jobs/view/3333333333/"
    assert _canonical_url(clean) == clean


def test_canonical_url_empty_string():
    assert _canonical_url("") == ""


def test_canonical_url_no_trailing_slash():
    # Path preserved exactly as-is (no slash added or removed)
    raw = "https://www.linkedin.com/jobs/view/4444444444?trk=foo"
    assert _canonical_url(raw) == "https://www.linkedin.com/jobs/view/4444444444"


def test_canonical_url_constructed_url_no_params():
    # Constructed fallback URLs (https://www.linkedin.com/jobs/view/<id>/) have no params
    url = "https://www.linkedin.com/jobs/view/5555555555/"
    assert _canonical_url(url) == url


def test_canonical_url_preserves_scheme_and_host():
    raw = "https://www.linkedin.com/jobs/view/6666666666/?mid=abc"
    result = _canonical_url(raw)
    assert result.startswith("https://www.linkedin.com")


def test_canonical_url_midtoken_stripped():
    # midToken is a common LinkedIn tracking param
    raw = (
        "https://www.linkedin.com/jobs/view/7777777777/"
        "?midToken=ABCD1234&midSig=XYZ&trk=jobs_jac"
    )
    assert _canonical_url(raw) == "https://www.linkedin.com/jobs/view/7777777777/"
