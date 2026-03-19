"""Tests for the _parse_salary function in sources/linkedin.py (Phase 11.3)."""

import pytest

from auto_apply.sources.linkedin import _parse_salary, _HOURS_PER_YEAR, _DAYS_PER_YEAR


# ── Annual salary formats ────────────────────────────────────────────────────

def test_annual_range_k_suffix():
    """£80k - £100k is parsed as an annual range."""
    mn, mx, text = _parse_salary("£80k - £100k")
    assert mn == 80_000.0
    assert mx == 100_000.0
    assert text == "£80k - £100k"


def test_annual_range_comma_thousands():
    """£80,000 - £100,000 per year is parsed correctly."""
    mn, mx, text = _parse_salary("£80,000 - £100,000 per year")
    assert mn == 80_000.0
    assert mx == 100_000.0


def test_annual_single_value():
    """Single annual salary populates salary_min only."""
    mn, mx, text = _parse_salary("£90,000")
    assert mn == 90_000.0
    assert mx is None


def test_annual_plus_suffix():
    """£80,000+ is treated as a single lower-bound value."""
    mn, mx, text = _parse_salary("£80,000+")
    assert mn == 80_000.0
    assert mx is None


def test_annual_slash_yr_range():
    """£80,000/yr - £100,000/yr extracts both bounds."""
    mn, mx, text = _parse_salary("£80,000/yr - £100,000/yr")
    assert mn == 80_000.0
    assert mx == 100_000.0


def test_annual_k_suffix_range_no_currency_on_max():
    """£85k - £95k (no £ on second value) extracts both bounds."""
    mn, mx, _ = _parse_salary("£85k - £95k")
    assert mn == 85_000.0
    assert mx == 95_000.0


def test_annual_uppercase_k_suffix():
    """£90K (uppercase K) is treated the same as £90k."""
    mn, mx, _ = _parse_salary("£90K")
    assert mn == 90_000.0
    assert mx is None


# ── Hourly formats ───────────────────────────────────────────────────────────

def test_hourly_per_hour_annualised():
    """£40 per hour is annualised by _HOURS_PER_YEAR."""
    mn, mx, _ = _parse_salary("£40 per hour")
    assert mn == pytest.approx(40.0 * _HOURS_PER_YEAR)
    assert mx is None


def test_hourly_slash_hr_annualised():
    """£40/hr is annualised by _HOURS_PER_YEAR."""
    mn, mx, _ = _parse_salary("£40/hr")
    assert mn == pytest.approx(40.0 * _HOURS_PER_YEAR)
    assert mx is None


def test_hourly_range_annualised():
    """£30 - £40 per hour produces annualised min and max."""
    mn, mx, _ = _parse_salary("£30 - £40 per hour")
    assert mn == pytest.approx(30.0 * _HOURS_PER_YEAR)
    assert mx == pytest.approx(40.0 * _HOURS_PER_YEAR)


def test_hourly_decimal_rate():
    """£37.50/hr handles a decimal hourly rate."""
    mn, mx, _ = _parse_salary("£37.50/hr")
    assert mn == pytest.approx(37.5 * _HOURS_PER_YEAR)
    assert mx is None


def test_hourly_ignores_embedded_annual_equivalent():
    """Annual equivalent mentioned in parentheses does not create a second bound."""
    # "£40/hr (circa £75,200/yr)" — 75,200 is outside the hourly plausible range
    mn, mx, _ = _parse_salary("£40/hr (circa £75,200 per year)")
    # Only 40 is in the hourly plausible range; 75200 is filtered out
    assert mn == pytest.approx(40.0 * _HOURS_PER_YEAR)
    assert mx is None


# ── Daily formats ────────────────────────────────────────────────────────────

def test_daily_per_day_annualised():
    """£300 per day is annualised by _DAYS_PER_YEAR."""
    mn, mx, _ = _parse_salary("£300 per day")
    assert mn == pytest.approx(300.0 * _DAYS_PER_YEAR)
    assert mx is None


def test_daily_slash_day_annualised():
    """£300/day is annualised by _DAYS_PER_YEAR."""
    mn, mx, _ = _parse_salary("£300/day")
    assert mn == pytest.approx(300.0 * _DAYS_PER_YEAR)
    assert mx is None


def test_daily_range_annualised():
    """£200 - £300 per day produces annualised min and max."""
    mn, mx, _ = _parse_salary("£200 - £300 per day")
    assert mn == pytest.approx(200.0 * _DAYS_PER_YEAR)
    assert mx == pytest.approx(300.0 * _DAYS_PER_YEAR)


# ── Vague / non-numeric salary strings ──────────────────────────────────────

def test_vague_competitive():
    """'Competitive' produces no numeric values but preserves salary_text."""
    mn, mx, text = _parse_salary("Competitive")
    assert mn is None
    assert mx is None
    assert text == "Competitive"


def test_vague_doe():
    """'DOE' (Depending On Experience) produces no numeric values."""
    mn, mx, text = _parse_salary("DOE")
    assert mn is None
    assert mx is None
    assert text == "DOE"


def test_vague_negotiable():
    """'Negotiable' produces no numeric values."""
    mn, mx, text = _parse_salary("Negotiable")
    assert mn is None
    assert mx is None
    assert text == "Negotiable"


def test_vague_not_specified():
    """'Not specified' produces no numeric values."""
    mn, mx, text = _parse_salary("Not specified")
    assert mn is None
    assert mx is None
    assert text == "Not specified"


# ── Non-GBP currencies ───────────────────────────────────────────────────────

def test_non_gbp_usd_stores_text_only():
    """$100,000 is kept as salary_text but produces no numeric GBP values."""
    mn, mx, text = _parse_salary("$100,000")
    assert mn is None
    assert mx is None
    assert text == "$100,000"


def test_non_gbp_eur_stores_text_only():
    """€80,000 is kept as salary_text but produces no numeric GBP values."""
    mn, mx, text = _parse_salary("€80,000")
    assert mn is None
    assert mx is None
    assert text == "€80,000"


# ── Empty / whitespace input ─────────────────────────────────────────────────

def test_empty_string_returns_none_tuple():
    """Empty string returns (None, None, None)."""
    mn, mx, text = _parse_salary("")
    assert mn is None
    assert mx is None
    assert text is None


def test_whitespace_only_returns_none_tuple():
    """Whitespace-only string returns (None, None, None)."""
    mn, mx, text = _parse_salary("   ")
    assert mn is None
    assert mx is None
    assert text is None


# ── Implausible / malformed values ───────────────────────────────────────────

def test_implausible_small_annual_returns_text_only():
    """A number too small to be a salary (e.g. '£50') produces no numeric values."""
    # 50 is below the annual plausible lower bound (1_000)
    mn, mx, text = _parse_salary("£50")
    assert mn is None
    assert mx is None
    assert text == "£50"


def test_preserves_original_text():
    """salary_text is always the original cleaned input, not a reformatted version."""
    raw = "  £95,000 - £110,000 per annum  "
    _, _, text = _parse_salary(raw)
    assert text == raw.strip()
