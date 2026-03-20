#  Orchestration Engine - Config Validator Tests (TDD)
#
#  Tests for the config_validator module.
#  These tests are expected to FAIL until the validator is implemented.
#
#  Depends on: backend.services.config_validator (not yet created)
#  Used by:    pytest

import pytest

from backend.services.config_validator import ConfigValidator, ConfigValidationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_config():
    """Return a minimal valid configuration dict."""
    return {
        "server": {
            "host": "0.0.0.0",
            "port": 5200,
            "cors_origins": [
                "http://localhost:5173",
                "http://localhost:5200",
            ],
        },
        "auth": {
            "secret_key": "a" * 64,
        },
    }


# ---------------------------------------------------------------------------
# Missing required fields
# ---------------------------------------------------------------------------

class TestMissingRequiredFields:
    """[R1] Required fields: server.host, server.port, auth.secret_key."""

    def test_missing_server_section(self):
        config = _valid_config()
        del config["server"]
        with pytest.raises(ConfigValidationError, match="server.host"):
            ConfigValidator(config).validate()

    def test_missing_server_host(self):
        config = _valid_config()
        del config["server"]["host"]
        with pytest.raises(ConfigValidationError, match="server.host"):
            ConfigValidator(config).validate()

    def test_missing_server_port(self):
        config = _valid_config()
        del config["server"]["port"]
        with pytest.raises(ConfigValidationError, match="server.port"):
            ConfigValidator(config).validate()

    def test_missing_auth_section(self):
        config = _valid_config()
        del config["auth"]
        with pytest.raises(ConfigValidationError, match="auth.secret_key"):
            ConfigValidator(config).validate()

    def test_missing_auth_secret_key(self):
        config = _valid_config()
        del config["auth"]["secret_key"]
        with pytest.raises(ConfigValidationError, match="auth.secret_key"):
            ConfigValidator(config).validate()


# ---------------------------------------------------------------------------
# Incorrect data types
# ---------------------------------------------------------------------------

class TestIncorrectDataTypes:
    """[R1] Type checks: port must be int, cors_origins must be list."""

    def test_port_as_string(self):
        config = _valid_config()
        config["server"]["port"] = "5200"
        with pytest.raises(ConfigValidationError, match="server.port"):
            ConfigValidator(config).validate()

    def test_port_as_float(self):
        config = _valid_config()
        config["server"]["port"] = 5200.5
        with pytest.raises(ConfigValidationError, match="server.port"):
            ConfigValidator(config).validate()

    def test_port_below_range(self):
        config = _valid_config()
        config["server"]["port"] = 0
        with pytest.raises(ConfigValidationError, match="server.port"):
            ConfigValidator(config).validate()

    def test_port_above_range(self):
        config = _valid_config()
        config["server"]["port"] = 70000
        with pytest.raises(ConfigValidationError, match="server.port"):
            ConfigValidator(config).validate()

    def test_cors_origins_as_string(self):
        config = _valid_config()
        config["server"]["cors_origins"] = "http://localhost:5173"
        with pytest.raises(ConfigValidationError, match="cors_origins"):
            ConfigValidator(config).validate()

    def test_cors_origins_contains_non_string(self):
        config = _valid_config()
        config["server"]["cors_origins"] = ["http://localhost:5173", 42]
        with pytest.raises(ConfigValidationError, match="cors_origins"):
            ConfigValidator(config).validate()


# ---------------------------------------------------------------------------
# Invalid auth.secret_key
# ---------------------------------------------------------------------------

class TestInvalidSecretKey:
    """[R1] secret_key: must be >= 32 chars, must not be a placeholder."""

    def test_secret_key_too_short(self):
        config = _valid_config()
        config["auth"]["secret_key"] = "short"
        with pytest.raises(ConfigValidationError, match="too short"):
            ConfigValidator(config).validate()

    def test_secret_key_exactly_31_chars(self):
        config = _valid_config()
        config["auth"]["secret_key"] = "a" * 31
        with pytest.raises(ConfigValidationError, match="too short"):
            ConfigValidator(config).validate()

    def test_secret_key_empty_string(self):
        config = _valid_config()
        config["auth"]["secret_key"] = ""
        with pytest.raises(ConfigValidationError, match="too short"):
            ConfigValidator(config).validate()

    def test_secret_key_placeholder_change_me(self):
        config = _valid_config()
        config["auth"]["secret_key"] = "CHANGE-ME-generate-a-random-64-char-string-here"
        with pytest.raises(ConfigValidationError, match="placeholder"):
            ConfigValidator(config).validate()

    def test_secret_key_placeholder_your_secret(self):
        config = _valid_config()
        config["auth"]["secret_key"] = "your-secret-key-here-padding-to-hit-32-chars"
        with pytest.raises(ConfigValidationError, match="placeholder"):
            ConfigValidator(config).validate()


# ---------------------------------------------------------------------------
# Fully valid configuration
# ---------------------------------------------------------------------------

class TestValidConfig:
    """[R1] A fully valid config must pass without errors."""

    def test_minimal_valid_config(self):
        config = _valid_config()
        errors = ConfigValidator(config).validate()
        assert errors is None or errors == []

    def test_valid_config_with_32_char_secret(self):
        config = _valid_config()
        config["auth"]["secret_key"] = "b" * 32
        errors = ConfigValidator(config).validate()
        assert errors is None or errors == []

    def test_valid_config_port_boundary_low(self):
        config = _valid_config()
        config["server"]["port"] = 1
        errors = ConfigValidator(config).validate()
        assert errors is None or errors == []

    def test_valid_config_port_boundary_high(self):
        config = _valid_config()
        config["server"]["port"] = 65535
        errors = ConfigValidator(config).validate()
        assert errors is None or errors == []

    def test_valid_config_empty_cors_origins(self):
        config = _valid_config()
        config["server"]["cors_origins"] = []
        errors = ConfigValidator(config).validate()
        assert errors is None or errors == []
