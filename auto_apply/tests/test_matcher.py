"""Tests for matcher.py scoring and JSON parsing logic."""

from unittest.mock import MagicMock, patch

import pytest

from auto_apply.matcher import score_job


def _make_mock_response(text: str) -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    return msg


def test_score_job_parses_valid_json_response():
    """score_job correctly parses a well-formed Claude JSON response."""
    mock_text = (
        '{"score": 85, "reasoning": "Good match", '
        '"matched_skills": ["Python"], "missing_skills": []}'
    )
    with patch("auto_apply.matcher._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _make_mock_response(mock_text)
        result = score_job(
            job_id=1,
            title="Data Engineer",
            company="Acme",
            description="Python ETL pipelines",
            salary_text="£80k",
            cv_text="Experienced Python developer",
            api_key="sk-ant-test",
        )

    assert result.score == 85
    assert result.reasoning == "Good match"
    assert "Python" in result.matched_skills
    assert result.missing_skills == []


def test_score_job_strips_markdown_code_fence():
    """score_job strips ```json ... ``` code fences before parsing."""
    mock_text = (
        '```json\n{"score": 60, "reasoning": "Partial match", '
        '"matched_skills": [], "missing_skills": ["Spark"]}\n```'
    )
    with patch("auto_apply.matcher._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _make_mock_response(mock_text)
        result = score_job(1, "Analyst", "Corp", "Spark jobs", "£50k", "some cv", "sk-ant-test")

    assert result.score == 60
    assert "Spark" in result.missing_skills


def test_score_job_strips_plain_code_fence():
    """score_job strips ``` ... ``` code fences (no language tag) before parsing."""
    mock_text = (
        '```\n{"score": 72, "reasoning": "Strong match", '
        '"matched_skills": ["SQL"], "missing_skills": []}\n```'
    )
    with patch("auto_apply.matcher._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _make_mock_response(mock_text)
        result = score_job(1, "Analyst", "Corp", "SQL jobs", "£60k", "some cv", "sk-ant-test")

    assert result.score == 72


def test_score_job_clamps_score_above_100():
    """score_job clamps scores above 100 to 100."""
    mock_text = '{"score": 150, "reasoning": "Extreme", "matched_skills": [], "missing_skills": []}'
    with patch("auto_apply.matcher._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _make_mock_response(mock_text)
        result = score_job(1, "Analyst", "Corp", "desc", "£50k", "cv", "sk-ant-test")

    assert result.score == 100


def test_score_job_clamps_score_below_0():
    """score_job clamps negative scores to 0."""
    mock_text = '{"score": -10, "reasoning": "Bad", "matched_skills": [], "missing_skills": []}'
    with patch("auto_apply.matcher._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _make_mock_response(mock_text)
        result = score_job(1, "Analyst", "Corp", "desc", "£50k", "cv", "sk-ant-test")

    assert result.score == 0


def test_score_job_returns_zero_on_unexpected_error():
    """score_job returns score 0 and includes error detail when API raises."""
    with patch("auto_apply.matcher._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.side_effect = Exception("unexpected API error")
        result = score_job(1, "Analyst", "Corp", "desc", "£50k", "cv", "sk-ant-test")

    assert result.score == 0
    assert "Scoring failed" in result.reasoning


def test_score_job_returns_zero_on_malformed_json():
    """score_job returns score 0 when Claude returns non-JSON text."""
    mock_text = "Sorry, I cannot score this job right now."
    with patch("auto_apply.matcher._get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.return_value = _make_mock_response(mock_text)
        result = score_job(1, "Analyst", "Corp", "desc", "£50k", "cv", "sk-ant-test")

    assert result.score == 0
