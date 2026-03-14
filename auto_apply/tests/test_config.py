"""Tests for config.py load/save helpers."""

import json
from unittest.mock import MagicMock, mock_open, patch

import pytest


def test_load_config_returns_empty_dict_when_file_missing():
    mock_path = MagicMock()
    mock_path.exists.return_value = False
    with patch("auto_apply.config.CONFIG_PATH", mock_path):
        from auto_apply.config import load_config
        result = load_config()
    assert result == {}


def test_load_config_returns_parsed_json_when_file_exists():
    config_data = {"claude_api_key": "sk-ant-test", "linkedin_email": "test@example.com"}
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    with patch("auto_apply.config.CONFIG_PATH", mock_path):
        with patch("builtins.open", mock_open(read_data=json.dumps(config_data))):
            from auto_apply.config import load_config
            result = load_config()
    assert result["claude_api_key"] == "sk-ant-test"
    assert result["linkedin_email"] == "test@example.com"


def test_load_config_returns_empty_dict_on_invalid_json():
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    with patch("auto_apply.config.CONFIG_PATH", mock_path):
        with patch("builtins.open", mock_open(read_data="not valid json {")):
            from auto_apply.config import load_config
            result = load_config()
    assert result == {}


def test_config_exists_returns_false_when_file_missing():
    mock_path = MagicMock()
    mock_path.exists.return_value = False
    with patch("auto_apply.config.CONFIG_PATH", mock_path):
        from auto_apply.config import config_exists
        assert config_exists() is False


def test_config_exists_returns_true_when_valid_config_present():
    config_data = {"claude_api_key": "sk-ant-test"}
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    with patch("auto_apply.config.CONFIG_PATH", mock_path):
        with patch("builtins.open", mock_open(read_data=json.dumps(config_data))):
            from auto_apply.config import config_exists
            assert config_exists() is True


def test_config_exists_returns_false_when_file_is_empty_json():
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    with patch("auto_apply.config.CONFIG_PATH", mock_path):
        with patch("builtins.open", mock_open(read_data="{}")):
            from auto_apply.config import config_exists
            # {} is falsy — config_exists should return False
            assert config_exists() is False
