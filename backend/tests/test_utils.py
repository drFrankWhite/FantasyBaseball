import pytest
from app.utils import (
    clean_numeric_string,
    normalize_name,
    validate_search_query,
    generate_fantasypros_player_url,
)

def test_clean_numeric_string():
    """Test the clean_numeric_string function handles various formats correctly."""

    # Test cases with input and expected output
    test_cases = [
        ('1,001.50', 1001.5),
        ('1,234', 1234.0),
        ('500', 500.0),
        ('0', 0.0),
        ('1,234,567.89', 1234567.89),
        ('  1,234.50  ', 1234.5),  # With whitespace
        ('10,000.00', 10000.0),
    ]

    for input_val, expected in test_cases:
        result = clean_numeric_string(input_val)
        assert result == expected, f"Failed for input '{input_val}': expected {expected}, got {result}"

def test_clean_numeric_string_edge_cases():
    """Test edge cases for clean_numeric_string function."""

    # Test None input
    assert clean_numeric_string(None) is None

    # Test empty string
    assert clean_numeric_string('') is None

    # Test whitespace only
    assert clean_numeric_string('   ') is None

def test_clean_numeric_string_invalid_input():
    """Test that invalid inputs raise appropriate exceptions."""

    with pytest.raises(ValueError):
        clean_numeric_string('invalid')

    with pytest.raises(ValueError):
        clean_numeric_string('1,00a.50')


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------

class TestNormalizeName:
    def test_basic_name(self):
        assert normalize_name("Aaron Judge") == "aaron judge"

    def test_removes_accents(self):
        assert normalize_name("Ronald Acuña") == "ronald acuna"

    def test_removes_jr_suffix(self):
        assert normalize_name("Ronald Acuña Jr.") == "ronald acuna"

    def test_removes_sr_suffix(self):
        assert normalize_name("Ken Griffey Sr.") == "ken griffey"

    def test_removes_roman_suffixes(self):
        assert normalize_name("Cal Ripken II") == "cal ripken"

    def test_hyphen_becomes_space(self):
        assert normalize_name("Pete Crow-Armstrong") == "pete crow armstrong"

    def test_empty_string(self):
        assert normalize_name("") == ""

    def test_none_input(self):
        assert normalize_name(None) == ""

    def test_strips_whitespace(self):
        assert normalize_name("  Aaron Judge  ") == "aaron judge"


# ---------------------------------------------------------------------------
# validate_search_query
# ---------------------------------------------------------------------------

class TestValidateSearchQuery:
    def test_valid_query(self):
        assert validate_search_query("Aaron Judge") == "Aaron Judge"

    def test_strips_whitespace(self):
        assert validate_search_query("  acuna  ") == "acuna"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            validate_search_query("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            validate_search_query("   ")

    def test_too_long_raises(self):
        with pytest.raises(ValueError):
            validate_search_query("a" * 101)

    def test_sql_injection_raises(self):
        with pytest.raises(ValueError):
            validate_search_query("'; DROP TABLE--")


# ---------------------------------------------------------------------------
# generate_fantasypros_player_url
# ---------------------------------------------------------------------------

class TestGenerateFantasyProsUrl:
    def test_simple_name(self):
        url = generate_fantasypros_player_url("Aaron Judge")
        assert "aaron-judge" in url

    def test_with_jr_suffix(self):
        url = generate_fantasypros_player_url("Bobby Witt Jr.")
        assert "bobby-witt-jr" in url

    def test_accented_name(self):
        url = generate_fantasypros_player_url("Ronald Acuña Jr.")
        assert "ronald-acuna-jr" in url