#  Orchestration Engine - RAG Sanitization Tests
#
#  Tests for FTS query sanitization in RAG tools.
#
#  Depends on: backend/tools/rag.py
#  Used by:    pytest


from backend.tools.rag import _sanitize_fts_query


class TestSanitizeFTSQuery:
    def test_simple_term(self):
        result = _sanitize_fts_query("Graphics")
        assert result == '"Graphics"'

    def test_strips_asterisk(self):
        result = _sanitize_fts_query("Graph*")
        assert "*" not in result
        assert result == '"Graph"'

    def test_strips_parentheses(self):
        result = _sanitize_fts_query("(foo)")
        assert "(" not in result
        assert ")" not in result

    def test_strips_plus(self):
        result = _sanitize_fts_query("foo+bar")
        assert "+" not in result

    def test_strips_caret(self):
        result = _sanitize_fts_query("foo^2")
        assert "^" not in result

    def test_strips_or_keyword(self):
        result = _sanitize_fts_query("foo OR bar")
        assert "OR" not in result
        assert result == '"foo bar"'

    def test_strips_and_keyword(self):
        result = _sanitize_fts_query("foo AND bar")
        assert "AND" not in result

    def test_strips_not_keyword(self):
        result = _sanitize_fts_query("NOT foo")
        assert "NOT" not in result

    def test_strips_near_keyword(self):
        result = _sanitize_fts_query("foo NEAR bar")
        assert "NEAR" not in result

    def test_strips_double_quotes(self):
        result = _sanitize_fts_query('foo"bar')
        # Double quotes are stripped by the regex, leaving "foo bar"
        assert result == '"foo bar"'

    def test_empty_after_sanitization(self):
        result = _sanitize_fts_query('*()+"^')
        assert result == ""

    def test_preserves_internal_dashes(self):
        result = _sanitize_fts_query("some-type-name")
        assert "some-type-name" in result

    def test_strips_leading_dash(self):
        result = _sanitize_fts_query("-foo")
        assert result == '"foo"'

    def test_wildcards_percent_underscore(self):
        # These aren't FTS operators but shouldn't cause issues
        result = _sanitize_fts_query("50%_complete")
        assert result == '"50%_complete"'
