"""Tests for gods/providers/response_validator.py"""

import pytest
from gods.providers.response_validator import (
    extract_json,
    validate_response,
    validate_verdict,
    validate_review,
)


class TestExtractJson:
    def test_clean_json(self):
        assert extract_json('{"key": "value"}') == {"key": "value"}

    def test_json_array(self):
        assert extract_json('[1, 2, 3]') == [1, 2, 3]

    def test_markdown_fenced(self):
        text = '```json\n{"verdict": "passed"}\n```'
        assert extract_json(text) == {"verdict": "passed"}

    def test_markdown_fenced_no_lang(self):
        text = '```\n{"verdict": "passed"}\n```'
        assert extract_json(text) == {"verdict": "passed"}

    def test_json_in_prose(self):
        text = 'Here is the result: {"verdict": "passed", "confidence": 0.9} as requested.'
        result = extract_json(text)
        assert result["verdict"] == "passed"

    def test_array_in_prose(self):
        text = 'Found these: ["item1", "item2"]'
        assert extract_json(text) == ["item1", "item2"]

    def test_empty_string(self):
        assert extract_json("") is None

    def test_none(self):
        assert extract_json(None) is None

    def test_no_json(self):
        assert extract_json("This is just plain text with no JSON at all") is None

    def test_nested_json(self):
        text = '{"outer": {"inner": "value"}}'
        result = extract_json(text)
        assert result["outer"]["inner"] == "value"


class TestValidateResponse:
    def test_valid_dict(self):
        data, errors = validate_response('{"verdict": "passed"}', required_fields=["verdict"])
        assert data == {"verdict": "passed"}
        assert errors == []

    def test_missing_required_field(self):
        data, errors = validate_response('{"other": "value"}', required_fields=["verdict"])
        assert data is not None
        assert len(errors) > 0
        assert "verdict" in str(errors)

    def test_array_normalized_to_first(self):
        data, errors = validate_response('[{"verdict": "passed"}]', required_fields=["verdict"])
        assert data == {"verdict": "passed"}
        assert errors == []

    def test_empty_array(self):
        data, errors = validate_response('[]')
        assert data is None
        assert len(errors) > 0

    def test_unparseable(self):
        data, errors = validate_response('not json at all')
        assert data is None
        assert len(errors) > 0

    def test_wrong_type(self):
        data, errors = validate_response('"just a string"', expected_type=dict)
        assert data is None


class TestValidateVerdict:
    def test_clean_passed(self):
        result = validate_verdict('{"verdict": "passed", "confidence": 0.9, "feedback": "Good"}')
        assert result["verdict"] == "passed"
        assert result["confidence"] == 0.9

    def test_clean_gaps_found(self):
        result = validate_verdict('{"verdict": "gaps_found", "feedback": "Missing tests"}')
        assert result["verdict"] == "gaps_found"

    def test_normalized_verdict_pass(self):
        result = validate_verdict('{"verdict": "approved"}')
        assert result["verdict"] == "passed"

    def test_normalized_verdict_fail(self):
        result = validate_verdict('{"verdict": "failed"}')
        assert result["verdict"] == "gaps_found"

    def test_prose_passed(self):
        result = validate_verdict("The task output satisfies all requirements. Everything looks correct.")
        assert result["verdict"] == "passed"

    def test_prose_failed(self):
        result = validate_verdict("The output is missing the required endpoint. There are gaps in coverage.")
        assert result["verdict"] == "gaps_found"

    def test_prose_unknown(self):
        result = validate_verdict("I'm not sure about this one.")
        assert result["verdict"] == "human_needed"

    def test_empty_response(self):
        result = validate_verdict("")
        assert result["verdict"] == "human_needed"

    def test_defaults_filled(self):
        result = validate_verdict('{"verdict": "passed"}')
        assert "confidence" in result
        assert "feedback" in result

    def test_markdown_fenced_json(self):
        text = 'Here is my analysis:\n```json\n{"verdict": "passed", "confidence": 0.95}\n```'
        result = validate_verdict(text)
        assert result["verdict"] == "passed"
        assert result["confidence"] == 0.95

    def test_array_response(self):
        result = validate_verdict('[{"verdict": "passed", "confidence": 0.8}]')
        assert result["verdict"] == "passed"


class TestValidateReview:
    def test_approved(self):
        result = validate_review('{"verdict": "approved", "feedback": "LGTM"}')
        assert result["verdict"] == "approved"

    def test_changes_requested(self):
        result = validate_review('{"verdict": "changes_requested", "feedback": "Fix imports"}')
        assert result["verdict"] == "changes_requested"

    def test_normalized_lgtm(self):
        result = validate_review('{"verdict": "lgtm"}')
        assert result["verdict"] == "approved"

    def test_prose_rejection(self):
        result = validate_review("There are several issues that need to be fixed before merging.")
        assert result["verdict"] == "changes_requested"

    def test_prose_approval(self):
        result = validate_review("Everything looks good, no issues found.")
        assert result["verdict"] == "approved"

    def test_empty(self):
        result = validate_review("")
        assert result["verdict"] == "approved"
        assert result["feedback"] == ""
