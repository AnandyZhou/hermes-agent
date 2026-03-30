"""Tests for Anthropic 'sensitive' stop_reason handling.

When Anthropic returns stop_reason='sensitive' (content policy filtering),
the agent should:
1. Not treat it as an invalid/empty response that triggers retries
2. Map it to finish_reason='content_filter'
3. Inject a user-facing message when content blocks are empty
4. Continue the conversation gracefully
"""

from types import SimpleNamespace

import pytest


class TestNormalizeAnthropicSensitive:
    """Tests for normalize_anthropic_response with stop_reason='sensitive'."""

    def test_sensitive_empty_content_injects_message(self):
        """When stop_reason='sensitive' and content is empty, inject a user-facing message."""
        from agent.anthropic_adapter import normalize_anthropic_response

        response = SimpleNamespace(
            content=[],
            stop_reason="sensitive",
        )

        assistant_msg, finish_reason = normalize_anthropic_response(response)

        assert finish_reason == "content_filter"
        assert assistant_msg.content is not None
        assert "filtered" in assistant_msg.content.lower()
        assert "content policy" in assistant_msg.content.lower()
        assert assistant_msg.tool_calls is None

    def test_sensitive_with_partial_text_preserves_content(self):
        """When stop_reason='sensitive' but some text was returned, preserve it."""
        from agent.anthropic_adapter import normalize_anthropic_response

        response = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Here is a partial answer..."),
            ],
            stop_reason="sensitive",
        )

        assistant_msg, finish_reason = normalize_anthropic_response(response)

        assert finish_reason == "content_filter"
        # The partial text should be preserved, not replaced
        assert assistant_msg.content == "Here is a partial answer..."

    def test_normal_end_turn_unchanged(self):
        """Normal end_turn responses are unaffected by the sensitive handling."""
        from agent.anthropic_adapter import normalize_anthropic_response

        response = SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Hello, world!"),
            ],
            stop_reason="end_turn",
        )

        assistant_msg, finish_reason = normalize_anthropic_response(response)

        assert finish_reason == "stop"
        assert assistant_msg.content == "Hello, world!"

    def test_sensitive_with_thinking_blocks(self):
        """Sensitive response with thinking blocks still works."""
        from agent.anthropic_adapter import normalize_anthropic_response

        response = SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking="Let me think about this..."),
            ],
            stop_reason="sensitive",
        )

        assistant_msg, finish_reason = normalize_anthropic_response(response)

        assert finish_reason == "content_filter"
        # Thinking was present but no text — should inject the filter message
        assert assistant_msg.content is not None
        assert "filtered" in assistant_msg.content.lower()
        # Reasoning should be preserved
        assert assistant_msg.reasoning is not None
        assert "think about this" in assistant_msg.reasoning

    def test_sensitive_maps_to_content_filter_in_stop_reason_map(self):
        """The stop_reason_map in normalize_anthropic_response includes 'sensitive'."""
        from agent.anthropic_adapter import normalize_anthropic_response

        # Even with content present, finish_reason should be content_filter
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="partial")],
            stop_reason="sensitive",
        )

        _, finish_reason = normalize_anthropic_response(response)
        assert finish_reason == "content_filter"

    def test_tool_use_stop_reason_unchanged(self):
        """tool_use stop_reason is not affected."""
        from agent.anthropic_adapter import normalize_anthropic_response

        response = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id="tool_123",
                    name="bash",
                    input={"command": "ls"},
                ),
            ],
            stop_reason="tool_use",
        )

        assistant_msg, finish_reason = normalize_anthropic_response(response)

        assert finish_reason == "tool_calls"
        assert assistant_msg.tool_calls is not None
        assert len(assistant_msg.tool_calls) == 1
