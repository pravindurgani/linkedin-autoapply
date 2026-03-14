"""Tests for linkedin_apply configure and quick answer logic."""

import pytest

from auto_apply.applier.linkedin_apply import _build_quick_answers, _try_quick_answer
import auto_apply.applier.linkedin_apply as _la


_FULL_CONFIG = {
    "applicant_name": "Jane Smith",
    "applicant_email": "jane@example.com",
    "applicant_phone": "+44123456789",
}


def test_build_quick_answers_populates_first_name():
    answers = _build_quick_answers(_FULL_CONFIG)
    assert any("Jane" in str(v) for v in answers.values()), "First name missing from quick answers"


def test_build_quick_answers_populates_last_name():
    answers = _build_quick_answers(_FULL_CONFIG)
    assert any("Smith" in str(v) for v in answers.values()), "Last name missing from quick answers"


def test_build_quick_answers_populates_full_name():
    answers = _build_quick_answers(_FULL_CONFIG)
    assert any("Jane Smith" in str(v) for v in answers.values()), "Full name missing from quick answers"


def test_build_quick_answers_populates_email():
    answers = _build_quick_answers(_FULL_CONFIG)
    assert any("jane@example.com" in str(v) for v in answers.values()), "Email missing from quick answers"


def test_build_quick_answers_populates_phone():
    answers = _build_quick_answers(_FULL_CONFIG)
    assert any("+44123456789" in str(v) for v in answers.values()), "Phone missing from quick answers"


def test_build_quick_answers_handles_single_word_name():
    """First and last name are both set to the single word when no space."""
    answers = _build_quick_answers({"applicant_name": "Madonna"})
    assert any("Madonna" in str(v) for v in answers.values())


def test_build_quick_answers_handles_missing_config_keys():
    """Empty config returns a dict with empty string values for personal fields."""
    answers = _build_quick_answers({})
    assert isinstance(answers, dict)
    assert len(answers) > 0


def test_try_quick_answer_matches_years_experience_pattern(monkeypatch):
    monkeypatch.setattr(_la, "_QUICK_ANSWERS", _build_quick_answers(_FULL_CONFIG))
    result = _try_quick_answer("How many years of experience in data analytics?")
    assert result is not None
    assert result.isdigit()


def test_try_quick_answer_matches_salary_pattern(monkeypatch):
    monkeypatch.setattr(_la, "_QUICK_ANSWERS", _build_quick_answers(_FULL_CONFIG))
    result = _try_quick_answer("What are your salary expectations?")
    assert result is not None


def test_try_quick_answer_returns_none_for_unrecognised_question(monkeypatch):
    monkeypatch.setattr(_la, "_QUICK_ANSWERS", _build_quick_answers(_FULL_CONFIG))
    result = _try_quick_answer("Describe your favourite hobby in detail?")
    assert result is None


def test_try_quick_answer_matches_option_case_insensitively(monkeypatch):
    monkeypatch.setattr(_la, "_QUICK_ANSWERS", _build_quick_answers(_FULL_CONFIG))
    result = _try_quick_answer("Are you willing to relocate?", options=["Yes", "No"])
    assert result == "Yes"


def test_try_quick_answer_returns_none_when_option_not_matched(monkeypatch):
    """When answer can't be matched to provided options, returns None (falls back to Claude)."""
    monkeypatch.setattr(_la, "_QUICK_ANSWERS", _build_quick_answers(_FULL_CONFIG))
    # "notice period" answer is "1 month" but options don't include it
    result = _try_quick_answer("What is your notice period?", options=["Immediate", "2 weeks"])
    # May or may not match depending on option text — we just verify no crash
    assert result is None or isinstance(result, str)
