"""Tests for setup wizard validation functions."""

import pytest

from auto_apply.setup_wizard import _validate_email, _validate_cv_path


def test_validate_email_accepts_standard_address():
    assert _validate_email("user@example.com") is True


def test_validate_email_accepts_subdomain_address():
    assert _validate_email("user@mail.example.co.uk") is True


def test_validate_email_accepts_plus_addressing():
    assert _validate_email("user+filter@example.com") is True


def test_validate_email_rejects_missing_at_sign():
    assert _validate_email("not-an-email") is False


def test_validate_email_rejects_missing_domain():
    assert _validate_email("user@") is False


def test_validate_email_rejects_missing_tld():
    assert _validate_email("user@domain") is False


def test_validate_email_rejects_empty_string():
    assert _validate_email("") is False


def test_validate_cv_path_rejects_nonexistent_file():
    is_valid, err = _validate_cv_path("/nonexistent/path/cv.pdf")
    assert is_valid is False
    assert "not found" in err.lower() or "nonexistent" in err.lower()


def test_validate_cv_path_rejects_non_pdf_extension(tmp_path):
    txt_file = tmp_path / "cv.txt"
    txt_file.write_text("not a pdf")
    is_valid, err = _validate_cv_path(str(txt_file))
    assert is_valid is False
    assert "PDF" in err


def test_validate_cv_path_rejects_corrupt_pdf(tmp_path):
    bad_pdf = tmp_path / "bad.pdf"
    bad_pdf.write_bytes(b"this is not valid pdf content")
    is_valid, _ = _validate_cv_path(str(bad_pdf))
    assert is_valid is False
