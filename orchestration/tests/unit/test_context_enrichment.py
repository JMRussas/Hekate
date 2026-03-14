#  Orchestration Engine - Context Enrichment Unit Tests
#
#  Tests for EnrichmentService XML generation, token budget truncation,
#  and graceful degradation on empty/malformed context store responses.
#
#  Depends on: backend/services/enrichment_service.py,
#              backend/services/context_store_client.py
#  Used by:    pytest

from unittest.mock import AsyncMock, patch

import pytest

from backend.services.context_store_client import ContextStoreResult, _extract_nodes
from backend.services.enrichment_service import (
    EnrichmentResult,
    EnrichmentService,
    _build_xml,
    _classify_nodes,
    _format_node,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_client(nodes: list[dict] | None, latency_ms: float = 10.0) -> AsyncMock:
    """Return a mock ContextStoreClient whose preview() returns the given nodes."""
    client = AsyncMock()
    if nodes is None:
        client.preview.return_value = None
    else:
        client.preview.return_value = ContextStoreResult(nodes=nodes, latency_ms=latency_ms)
    return client


def _enabled_service(
    client,
    max_tokens: int = 2000,
    include_prior_outcomes: bool = True,
) -> EnrichmentService:
    """Build an EnrichmentService with enrichment enabled and a mock client."""
    with patch(
        "backend.services.enrichment_service.cfg",
        side_effect=lambda key, default=None: {
            "context_enrichment.enabled": True,
            "context_enrichment.max_context_tokens": max_tokens,
            "context_enrichment.include_prior_outcomes": include_prior_outcomes,
        }.get(key, default),
    ):
        svc = EnrichmentService(client=client)
    return svc


def _disabled_service(client) -> EnrichmentService:
    """Build an EnrichmentService with enrichment disabled."""
    with patch(
        "backend.services.enrichment_service.cfg",
        side_effect=lambda key, default=None: {
            "context_enrichment.enabled": False,
            "context_enrichment.max_context_tokens": 2000,
            "context_enrichment.include_prior_outcomes": True,
        }.get(key, default),
    ):
        svc = EnrichmentService(client=client)
    return svc


# ---------------------------------------------------------------------------
# Test: XML generation — node classification and tag structure
# ---------------------------------------------------------------------------

class TestXmlGeneration:
    """Nodes are classified into the correct XML sections and rendered properly."""

    @pytest.mark.asyncio
    async def test_gotcha_node_goes_to_relevant_gotchas(self):
        nodes = [{"type": "gotcha", "title": "Thread safety", "content": "Use locks."}]
        client = _make_client(nodes)
        svc = _enabled_service(client)

        result = await svc.enrich("some query")

        assert result is not None
        assert "<relevant_gotchas>" in result.xml_block
        assert "Thread safety" in result.xml_block

    @pytest.mark.asyncio
    async def test_convention_node_goes_to_project_conventions(self):
        nodes = [{"type": "architecture", "title": "DI rule", "content": "Always inject."}]
        client = _make_client(nodes)
        svc = _enabled_service(client)

        result = await svc.enrich("some query")

        assert result is not None
        assert "<project_conventions>" in result.xml_block
        assert "DI rule" in result.xml_block

    @pytest.mark.asyncio
    async def test_outcome_node_goes_to_prior_outcomes(self):
        nodes = [{"type": "outcome", "title": "Migration result", "content": "Worked fine."}]
        client = _make_client(nodes)
        svc = _enabled_service(client, include_prior_outcomes=True)

        result = await svc.enrich("some query")

        assert result is not None
        assert "<prior_outcomes>" in result.xml_block

    @pytest.mark.asyncio
    async def test_file_path_node_goes_to_affected_file_context(self):
        nodes = [{"filePath": "src/app.py", "title": "App entry", "content": "FastAPI app."}]
        client = _make_client(nodes)
        svc = _enabled_service(client)

        result = await svc.enrich("some query")

        assert result is not None
        assert "<affected_file_context>" in result.xml_block
        assert "src/app.py" in result.xml_block

    @pytest.mark.asyncio
    async def test_all_four_sections_present_when_each_type_provided(self):
        nodes = [
            {"type": "gotcha",       "content": "Watch out for X."},
            {"type": "convention",   "content": "Always do Y."},
            {"type": "outcome",      "content": "Z was completed."},
            {"filePath": "a/b.py",   "content": "Module context."},
        ]
        client = _make_client(nodes)
        svc = _enabled_service(client, include_prior_outcomes=True)

        result = await svc.enrich("some query")

        assert result is not None
        for tag in ("<relevant_gotchas>", "<project_conventions>",
                    "<prior_outcomes>", "<affected_file_context>"):
            assert tag in result.xml_block

    @pytest.mark.asyncio
    async def test_xml_tags_are_properly_closed(self):
        nodes = [{"type": "gotcha", "content": "Something to avoid."}]
        client = _make_client(nodes)
        svc = _enabled_service(client)

        result = await svc.enrich("some query")

        assert result is not None
        assert "</relevant_gotchas>" in result.xml_block

    @pytest.mark.asyncio
    async def test_title_and_content_formatted_with_colon(self):
        nodes = [{"type": "gotcha", "title": "Deadlock", "content": "Acquire locks in order."}]
        client = _make_client(nodes)
        svc = _enabled_service(client)

        result = await svc.enrich("some query")

        assert result is not None
        assert "Deadlock: Acquire locks in order." in result.xml_block

    @pytest.mark.asyncio
    async def test_node_with_title_only_renders_title(self):
        nodes = [{"type": "warning", "title": "OOM risk"}]
        client = _make_client(nodes)
        svc = _enabled_service(client)

        result = await svc.enrich("some query")

        assert result is not None
        assert "OOM risk" in result.xml_block

    @pytest.mark.asyncio
    async def test_node_with_content_only_renders_content(self):
        nodes = [{"type": "pattern", "content": "Use the repository pattern."}]
        client = _make_client(nodes)
        svc = _enabled_service(client)

        result = await svc.enrich("some query")

        assert result is not None
        assert "Use the repository pattern." in result.xml_block

    @pytest.mark.asyncio
    async def test_enrichment_result_has_correct_metadata(self):
        nodes = [{"type": "gotcha", "content": "Watch out."}]
        client = _make_client(nodes, latency_ms=42.0)
        svc = _enabled_service(client)

        result = await svc.enrich("query")

        assert result is not None
        assert isinstance(result, EnrichmentResult)
        assert result.node_count == 1
        assert result.tokens_used > 0
        assert result.latency_ms == 42.0


# ---------------------------------------------------------------------------
# Test: Token budget truncation
# ---------------------------------------------------------------------------

class TestTokenBudgetTruncation:
    """Content exceeding max_context_tokens is truncated by dropping lower-priority nodes."""

    def test_all_nodes_fit_when_under_budget(self):
        sections = {
            "project_conventions": [{"title": "A", "content": "Short."}],
            "relevant_gotchas":    [{"title": "B", "content": "Also short."}],
            "prior_outcomes":      [],
            "affected_file_context": [],
        }
        xml_block, tokens_used, node_count = _build_xml(sections, max_tokens=2000, include_prior_outcomes=True)
        assert "A" in xml_block
        assert "B" in xml_block
        assert node_count == 2
        assert tokens_used > 0

    def test_over_budget_drops_lower_priority_nodes(self):
        """With a tiny token budget, lower-priority sections are dropped first."""
        # Generate a node that exceeds the budget once rendered
        long_content = "x" * 500  # 500 chars → ~125 tokens
        sections = {
            "relevant_gotchas":      [{"content": "Gotcha always first."}],
            "project_conventions":   [{"content": long_content}],
            "prior_outcomes":        [{"content": long_content}],
            "affected_file_context": [{"content": long_content}],
        }
        # Budget: 30 tokens → 120 chars — only the gotcha line should fit
        xml_block, tokens_used, node_count = _build_xml(sections, max_tokens=30, include_prior_outcomes=True)

        assert "Gotcha always first." in xml_block
        assert tokens_used <= 35  # allow slight overhead for tags

    def test_zero_budget_returns_empty(self):
        sections = {
            "relevant_gotchas":    [{"content": "Something."}],
            "project_conventions": [{"content": "Another thing."}],
            "prior_outcomes":      [],
            "affected_file_context": [],
        }
        xml_block, tokens_used, node_count = _build_xml(sections, max_tokens=0, include_prior_outcomes=True)
        assert xml_block == ""
        assert tokens_used == 0
        assert node_count == 0

    def test_prior_outcomes_excluded_when_disabled(self):
        sections = {
            "relevant_gotchas":    [],
            "project_conventions": [],
            "prior_outcomes":      [{"content": "Past result."}],
            "affected_file_context": [],
        }
        xml_block, _, _ = _build_xml(sections, max_tokens=2000, include_prior_outcomes=False)
        assert "prior_outcomes" not in xml_block
        assert "Past result." not in xml_block

    @pytest.mark.asyncio
    async def test_service_respects_max_tokens_config(self):
        """EnrichmentService propagates max_tokens to _build_xml, truncating output."""
        # 10 nodes each with ~50-char content → well over a 5-token budget
        nodes = [{"type": "gotcha", "content": f"Avoid mistake number {i} carefully."} for i in range(10)]
        client = _make_client(nodes)
        svc = _enabled_service(client, max_tokens=5)

        result = await svc.enrich("query")

        # Either truncated to within budget or returns None if nothing fits
        if result is not None:
            assert result.tokens_used <= 10  # small tolerance

    @pytest.mark.asyncio
    async def test_prior_outcomes_excluded_when_include_prior_outcomes_false(self):
        nodes = [
            {"type": "outcome", "content": "Prior finding."},
            {"type": "gotcha",  "content": "Real gotcha."},
        ]
        client = _make_client(nodes)
        svc = _enabled_service(client, include_prior_outcomes=False)

        result = await svc.enrich("query")

        assert result is not None
        assert "Prior finding." not in result.xml_block
        assert "Real gotcha." in result.xml_block


# ---------------------------------------------------------------------------
# Test: Graceful degradation on empty/malformed responses
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    """Service returns None without raising when the context store fails or returns unusable data."""

    @pytest.mark.asyncio
    async def test_disabled_service_returns_none_without_calling_client(self):
        client = _make_client([{"type": "gotcha", "content": "Something."}])
        svc = _disabled_service(client)

        result = await svc.enrich("query")

        assert result is None
        client.preview.assert_not_called()

    @pytest.mark.asyncio
    async def test_client_returns_none_yields_none(self):
        client = _make_client(None)
        svc = _enabled_service(client)

        result = await svc.enrich("query")

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_nodes_list_yields_none(self):
        client = _make_client([])
        svc = _enabled_service(client)

        result = await svc.enrich("query")

        assert result is None

    @pytest.mark.asyncio
    async def test_all_nodes_missing_content_yields_none(self):
        # Nodes with no title, no content, no body, no summary — _format_node returns ""
        nodes = [{"type": "gotcha"}, {"type": "convention"}]
        client = _make_client(nodes)
        svc = _enabled_service(client)

        result = await svc.enrich("query")

        assert result is None

    @pytest.mark.asyncio
    async def test_unrecognised_type_with_prior_outcomes_disabled_yields_none(self):
        """Nodes of unknown types are dropped when include_prior_outcomes=False."""
        nodes = [{"type": "unknown_type", "content": "Some content."}]
        client = _make_client(nodes)
        svc = _enabled_service(client, include_prior_outcomes=False)

        result = await svc.enrich("query")

        assert result is None

    @pytest.mark.asyncio
    async def test_unrecognised_type_with_prior_outcomes_enabled_is_included(self):
        """Unknown types fall through to prior_outcomes when that section is enabled."""
        nodes = [{"type": "unknown_type", "content": "Unclassified finding."}]
        client = _make_client(nodes)
        svc = _enabled_service(client, include_prior_outcomes=True)

        result = await svc.enrich("query")

        assert result is not None
        assert "Unclassified finding." in result.xml_block

    @pytest.mark.asyncio
    async def test_malformed_node_dict_with_no_known_fields_is_skipped(self):
        nodes = [
            {"type": "gotcha", "unknownField": "irrelevant"},   # no content/title
            {"type": "gotcha", "content": "Valid gotcha."},
        ]
        client = _make_client(nodes)
        svc = _enabled_service(client)

        result = await svc.enrich("query")

        assert result is not None
        assert "Valid gotcha." in result.xml_block

    @pytest.mark.asyncio
    async def test_non_dict_nodes_in_api_response_filtered_by_extract_nodes(self):
        """_extract_nodes (tested separately) strips non-dict items before the service sees them.

        This test verifies the full pipeline: a raw API response list containing mixed
        types arrives at _extract_nodes, which filters to dicts only, so the service
        receives a clean nodes list and produces a valid result.
        """
        raw_api_response = [None, "bad", 42, {"type": "gotcha", "content": "Good node."}]
        # Simulate what ContextStoreClient does: filter via _extract_nodes before returning.
        clean_nodes = _extract_nodes(raw_api_response)
        client = AsyncMock()
        client.preview.return_value = ContextStoreResult(nodes=clean_nodes, latency_ms=5.0)
        svc = _enabled_service(client)

        result = await svc.enrich("query")

        assert result is not None
        assert "Good node." in result.xml_block


# ---------------------------------------------------------------------------
# Test: _extract_nodes response normalisation
# ---------------------------------------------------------------------------

class TestExtractNodes:
    """_extract_nodes handles the various shapes the context store API might return."""

    def test_list_response_returned_as_is(self):
        data = [{"type": "gotcha", "content": "A"}, {"type": "pattern", "content": "B"}]
        assert _extract_nodes(data) == data

    def test_dict_with_nodes_key(self):
        data = {"nodes": [{"content": "x"}], "meta": "ignored"}
        assert _extract_nodes(data) == [{"content": "x"}]

    def test_dict_with_results_key(self):
        data = {"results": [{"content": "y"}]}
        assert _extract_nodes(data) == [{"content": "y"}]

    def test_dict_with_items_key(self):
        data = {"items": [{"content": "z"}]}
        assert _extract_nodes(data) == [{"content": "z"}]

    def test_dict_with_data_key(self):
        data = {"data": [{"content": "w"}]}
        assert _extract_nodes(data) == [{"content": "w"}]

    def test_empty_list_returns_empty(self):
        assert _extract_nodes([]) == []

    def test_empty_dict_returns_empty(self):
        assert _extract_nodes({}) == []

    def test_none_returns_empty(self):
        assert _extract_nodes(None) == []

    def test_string_returns_empty(self):
        assert _extract_nodes("bad response") == []

    def test_non_dict_items_in_list_are_filtered(self):
        data = [{"content": "good"}, "bad", None, 42]
        assert _extract_nodes(data) == [{"content": "good"}]

    def test_non_dict_items_under_nodes_key_are_filtered(self):
        data = {"nodes": [{"content": "good"}, "bad", None]}
        assert _extract_nodes(data) == [{"content": "good"}]

    def test_nodes_key_takes_priority_over_results(self):
        data = {"nodes": [{"content": "from nodes"}], "results": [{"content": "from results"}]}
        result = _extract_nodes(data)
        assert result == [{"content": "from nodes"}]


# ---------------------------------------------------------------------------
# Test: _classify_nodes section assignment
# ---------------------------------------------------------------------------

class TestClassifyNodes:
    """Node type strings map to the expected XML section."""

    def test_gotcha_types_all_go_to_relevant_gotchas(self):
        for t in ("gotcha", "warning", "pitfall", "issue", "bug"):
            sections = _classify_nodes([{"type": t, "content": "x"}], include_prior_outcomes=True)
            assert sections["relevant_gotchas"], f"type={t!r} should map to relevant_gotchas"

    def test_convention_types_all_go_to_project_conventions(self):
        for t in ("architecture", "decision", "convention", "pattern", "constraint"):
            sections = _classify_nodes([{"type": t, "content": "x"}], include_prior_outcomes=True)
            assert sections["project_conventions"], f"type={t!r} should map to project_conventions"

    def test_outcome_types_all_go_to_prior_outcomes(self):
        for t in ("outcome", "finding", "result", "discovery", "reference"):
            sections = _classify_nodes([{"type": t, "content": "x"}], include_prior_outcomes=True)
            assert sections["prior_outcomes"], f"type={t!r} should map to prior_outcomes"

    def test_file_path_takes_priority_over_type(self):
        node = {"type": "gotcha", "filePath": "src/foo.py", "content": "x"}
        sections = _classify_nodes([node], include_prior_outcomes=True)
        assert sections["affected_file_context"] == [node]
        assert sections["relevant_gotchas"] == []

    def test_node_appears_in_at_most_one_section(self):
        node = {"type": "gotcha", "content": "x"}
        sections = _classify_nodes([node], include_prior_outcomes=True)
        total = sum(len(v) for v in sections.values())
        assert total == 1

    def test_type_matching_is_case_insensitive(self):
        sections = _classify_nodes([{"type": "GOTCHA", "content": "x"}], include_prior_outcomes=True)
        assert sections["relevant_gotchas"]

    def test_nodetype_field_used_when_type_absent(self):
        sections = _classify_nodes([{"nodeType": "warning", "content": "x"}], include_prior_outcomes=True)
        assert sections["relevant_gotchas"]


# ---------------------------------------------------------------------------
# Test: _format_node rendering
# ---------------------------------------------------------------------------

class TestFormatNode:
    """_format_node produces the expected string for each section type."""

    def test_title_and_content_joined_with_colon(self):
        node = {"title": "Rule", "content": "Do this."}
        assert _format_node(node, "project_conventions") == "Rule: Do this."

    def test_title_only(self):
        node = {"title": "Only title"}
        assert _format_node(node, "project_conventions") == "Only title"

    def test_content_only(self):
        node = {"content": "Only content."}
        assert _format_node(node, "relevant_gotchas") == "Only content."

    def test_empty_node_returns_empty_string(self):
        assert _format_node({}, "project_conventions") == ""

    def test_file_context_includes_path_prefix(self):
        node = {"filePath": "src/app.py", "title": "Entry", "content": "FastAPI."}
        line = _format_node(node, "affected_file_context")
        assert "[src/app.py]" in line
        assert "Entry" in line

    def test_name_field_used_when_title_absent(self):
        node = {"name": "MyName", "content": "Some content."}
        assert _format_node(node, "project_conventions") == "MyName: Some content."

    def test_text_field_used_when_content_absent(self):
        node = {"title": "T", "text": "Body text."}
        assert _format_node(node, "relevant_gotchas") == "T: Body text."

    def test_whitespace_stripped_from_content(self):
        node = {"content": "  Leading and trailing.  "}
        assert _format_node(node, "project_conventions") == "Leading and trailing."
