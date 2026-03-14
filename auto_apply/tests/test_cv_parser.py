"""Tests for cv_parser.py extraction and caching logic."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_apply.cv_parser import extract_cv_text


def test_extract_cv_text_returns_cached_content_without_calling_pdfplumber(tmp_path, monkeypatch):
    """When cache file exists, returns cached text without touching the PDF."""
    cache_file = tmp_path / "cv_text.txt"
    cache_file.write_text("cached CV content", encoding="utf-8")
    monkeypatch.setattr("auto_apply.cv_parser.CV_CACHE_PATH", cache_file)

    with patch("pdfplumber.open") as mock_pdf:
        result = extract_cv_text()

    assert result == "cached CV content"
    mock_pdf.assert_not_called()


def test_extract_cv_text_force_bypasses_cache(tmp_path, monkeypatch):
    """force=True re-extracts even when cache exists."""
    cache_file = tmp_path / "cv_text.txt"
    cache_file.write_text("stale cached content", encoding="utf-8")
    monkeypatch.setattr("auto_apply.cv_parser.CV_CACHE_PATH", cache_file)

    fake_pdf_path = tmp_path / "cv.pdf"
    fake_pdf_path.write_bytes(b"%PDF-1.4")

    mock_page = MagicMock()
    mock_page.extract_text.return_value = "fresh CV content"
    mock_pdf_ctx = MagicMock()
    mock_pdf_ctx.__enter__ = MagicMock(return_value=MagicMock(pages=[mock_page]))
    mock_pdf_ctx.__exit__ = MagicMock(return_value=False)

    with patch("pdfplumber.open", return_value=mock_pdf_ctx):
        result = extract_cv_text(cv_path=str(fake_pdf_path), force=True)

    assert result == "fresh CV content"


def test_extract_cv_text_raises_file_not_found_for_missing_cv(tmp_path, monkeypatch):
    """FileNotFoundError raised when cv_path points to nonexistent file."""
    cache_file = tmp_path / "cv_text.txt"  # does not exist
    monkeypatch.setattr("auto_apply.cv_parser.CV_CACHE_PATH", cache_file)

    with pytest.raises(FileNotFoundError, match="CV not found"):
        extract_cv_text(cv_path="/nonexistent/path/cv.pdf")


def test_extract_cv_text_writes_extracted_text_to_cache(tmp_path, monkeypatch):
    """Extracted PDF text is written to CV_CACHE_PATH."""
    cache_file = tmp_path / "cv_text.txt"
    monkeypatch.setattr("auto_apply.cv_parser.CV_CACHE_PATH", cache_file)

    fake_pdf_path = tmp_path / "cv.pdf"
    fake_pdf_path.write_bytes(b"%PDF-1.4")

    mock_page = MagicMock()
    mock_page.extract_text.return_value = "extracted text"
    mock_pdf_ctx = MagicMock()
    mock_pdf_ctx.__enter__ = MagicMock(return_value=MagicMock(pages=[mock_page]))
    mock_pdf_ctx.__exit__ = MagicMock(return_value=False)

    with patch("pdfplumber.open", return_value=mock_pdf_ctx):
        extract_cv_text(cv_path=str(fake_pdf_path))

    assert cache_file.exists()
    assert cache_file.read_text(encoding="utf-8") == "extracted text"


def test_extract_cv_text_loads_cv_path_from_config_when_not_provided(tmp_path, monkeypatch):
    """When cv_path is None, cv_path is loaded from config.json."""
    cache_file = tmp_path / "cv_text.txt"
    monkeypatch.setattr("auto_apply.cv_parser.CV_CACHE_PATH", cache_file)

    fake_pdf_path = tmp_path / "cv.pdf"
    fake_pdf_path.write_bytes(b"%PDF-1.4")

    mock_page = MagicMock()
    mock_page.extract_text.return_value = "config-sourced CV text"
    mock_pdf_ctx = MagicMock()
    mock_pdf_ctx.__enter__ = MagicMock(return_value=MagicMock(pages=[mock_page]))
    mock_pdf_ctx.__exit__ = MagicMock(return_value=False)

    with patch("auto_apply.config.load_config", return_value={"cv_path": str(fake_pdf_path)}):
        with patch("pdfplumber.open", return_value=mock_pdf_ctx):
            result = extract_cv_text()

    assert result == "config-sourced CV text"
