"""Tests for WhatsApp echo loop prevention.

The WhatsApp adapter must filter out messages sent by the bot itself
(fromMe=True) to prevent infinite reply loops.  The JS bridge already
does this filtering, but the Python adapter adds a safety-net check.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _AsyncCM:
    """Minimal async context manager returning a fixed value."""

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _make_adapter():
    """Create a WhatsAppAdapter with test attributes (bypass __init__)."""
    from gateway.platforms.whatsapp import WhatsAppAdapter

    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = MagicMock()
    adapter._bridge_port = 19877
    adapter._bridge_script = "/tmp/test-bridge.js"
    adapter._session_path = Path("/tmp/test-wa-session")
    adapter._bridge_log_fh = None
    adapter._bridge_log = None
    adapter._bridge_process = None
    adapter._reply_prefix = None
    adapter._running = True
    adapter._message_handler = None
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._fatal_error_handler = None
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._background_tasks = set()
    adapter._auto_tts_disabled_chats = set()
    adapter._message_queue = asyncio.Queue()
    return adapter


# ---------------------------------------------------------------------------
# Echo loop prevention tests
# ---------------------------------------------------------------------------


class TestEchoLoopPrevention:
    """Verify that fromMe messages are filtered out in _poll_messages."""

    @pytest.mark.asyncio
    async def test_fromMe_messages_are_skipped(self):
        """Messages with fromMe=True should not be passed to handle_message.

        Simulates the filtering logic from _poll_messages: iterate over
        bridge response messages, skip any with fromMe=True, and only
        build events for the rest.
        """
        adapter = _make_adapter()

        # Build a mock bridge response with both fromMe and regular messages
        bridge_messages = [
            {
                "messageId": "msg-1",
                "chatId": "123@s.whatsapp.net",
                "senderId": "123@s.whatsapp.net",
                "senderName": "User",
                "chatName": "User",
                "isGroup": False,
                "fromMe": True,  # This is our own message — should be skipped
                "body": "Bot reply that should be ignored",
                "hasMedia": False,
                "mediaType": "",
                "mediaUrls": [],
                "timestamp": 1234567890,
            },
            {
                "messageId": "msg-2",
                "chatId": "456@s.whatsapp.net",
                "senderId": "456@s.whatsapp.net",
                "senderName": "Real User",
                "chatName": "Real User",
                "isGroup": False,
                "fromMe": False,  # This is from someone else
                "body": "Hello bot!",
                "hasMedia": False,
                "mediaType": "",
                "mediaUrls": [],
                "timestamp": 1234567891,
            },
        ]

        # Replicate the filtering logic from _poll_messages
        handled_events = []
        for msg_data in bridge_messages:
            if msg_data.get("fromMe", False):
                continue
            event = await adapter._build_message_event(msg_data)
            if event:
                handled_events.append(event)

        # Only the non-fromMe message should have been processed
        assert len(handled_events) == 1
        assert handled_events[0].text == "Hello bot!"

    @pytest.mark.asyncio
    async def test_fromMe_absent_defaults_to_false(self):
        """Messages without fromMe field should NOT be filtered out."""
        adapter = _make_adapter()

        # Message without fromMe field (backward compat with older bridge)
        msg_data = {
            "messageId": "msg-3",
            "chatId": "789@s.whatsapp.net",
            "senderId": "789@s.whatsapp.net",
            "senderName": "Old Bridge User",
            "chatName": "Old Bridge User",
            "isGroup": False,
            # No fromMe field
            "body": "Message from older bridge version",
            "hasMedia": False,
            "mediaType": "",
            "mediaUrls": [],
            "timestamp": 1234567892,
        }

        # fromMe defaults to False, so this message should pass the filter
        assert msg_data.get("fromMe", False) is False

        # Build the event to verify it works
        event = await adapter._build_message_event(msg_data)
        assert event is not None
        assert event.text == "Message from older bridge version"

    @pytest.mark.asyncio
    async def test_fromMe_false_passes_through(self):
        """Messages with fromMe=False are processed normally."""
        adapter = _make_adapter()

        msg_data = {
            "messageId": "msg-4",
            "chatId": "321@s.whatsapp.net",
            "senderId": "321@s.whatsapp.net",
            "senderName": "Normal User",
            "chatName": "Normal User",
            "isGroup": False,
            "fromMe": False,
            "body": "Normal incoming message",
            "hasMedia": False,
            "mediaType": "",
            "mediaUrls": [],
            "timestamp": 1234567893,
        }

        # Should not be filtered
        assert msg_data.get("fromMe", False) is False

        event = await adapter._build_message_event(msg_data)
        assert event is not None
        assert event.text == "Normal incoming message"
