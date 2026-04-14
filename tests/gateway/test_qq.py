"""Tests for the QQ Bot gateway integration."""

import asyncio
import json
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

try:
    import websockets
    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False


class TestPlatformEnum(unittest.TestCase):
    def test_qq_in_platform_enum(self):
        from gateway.config import Platform

        self.assertEqual(Platform.QQ.value, "qq")


class TestConfigEnvOverrides(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_12345",
        "QQ_CLIENT_SECRET": "secret_abc",
    }, clear=False)
    def test_qq_config_loaded_from_env(self):
        from gateway.config import GatewayConfig, Platform, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)

        self.assertIn(Platform.QQ, config.platforms)
        self.assertTrue(config.platforms[Platform.QQ].enabled)
        self.assertEqual(config.platforms[Platform.QQ].extra["app_id"], "app_12345")
        self.assertEqual(config.platforms[Platform.QQ].extra["client_secret"], "secret_abc")

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_12345",
        "QQ_CLIENT_SECRET": "secret_abc",
        "QQ_MARKDOWN_SUPPORT": "false",
        "QQ_GROUP_POLICY": "allowlist",
    }, clear=False)
    def test_qq_optional_config(self):
        from gateway.config import GatewayConfig, Platform, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)

        self.assertFalse(config.platforms[Platform.QQ].extra["markdown_support"])
        self.assertEqual(config.platforms[Platform.QQ].extra["group_policy"], "allowlist")

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_12345",
        "QQ_CLIENT_SECRET": "secret_abc",
        "QQ_GROUP_ALLOWLIST": "group_a,group_b",
    }, clear=False)
    def test_qq_group_allowlist_parsed(self):
        from gateway.config import GatewayConfig, Platform, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)

        self.assertEqual(
            config.platforms[Platform.QQ].extra["group_allowlist"],
            ["group_a", "group_b"],
        )

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_12345",
        "QQ_CLIENT_SECRET": "secret_abc",
        "QQ_HOME_CHANNEL": "user_openid_xxx",
    }, clear=False)
    def test_qq_home_channel_loaded(self):
        from gateway.config import GatewayConfig, Platform, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)

        home = config.platforms[Platform.QQ].home_channel
        self.assertIsNotNone(home)
        self.assertEqual(home.chat_id, "user_openid_xxx")
        self.assertEqual(home.platform, Platform.QQ)

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_12345",
        "QQ_CLIENT_SECRET": "secret_abc",
    }, clear=False)
    def test_qq_in_connected_platforms(self):
        from gateway.config import GatewayConfig, Platform, _apply_env_overrides

        config = GatewayConfig()
        _apply_env_overrides(config)

        self.assertIn(Platform.QQ, config.get_connected_platforms())


class TestGatewayIntegration(unittest.TestCase):
    def test_qq_in_adapter_factory(self):
        source = Path("gateway/run.py").read_text(encoding="utf-8")
        self.assertIn("Platform.QQ", source)
        self.assertIn("QQBotAdapter", source)

    def test_qq_in_authorization_maps(self):
        source = Path("gateway/run.py").read_text(encoding="utf-8")
        self.assertIn("QQ_ALLOWED_USERS", source)
        self.assertIn("QQ_ALLOW_ALL_USERS", source)

    def test_qq_toolset_exists(self):
        from toolsets import TOOLSETS

        self.assertIn("hermes-qq", TOOLSETS)
        self.assertIn("hermes-qq", TOOLSETS["hermes-gateway"]["includes"])

    def test_qq_in_cron_platform_map(self):
        source = Path("cron/scheduler.py").read_text(encoding="utf-8")
        self.assertIn('"qq": Platform.QQ', source)

    def test_qq_in_send_message_platform_map(self):
        source = Path("tools/send_message_tool.py").read_text(encoding="utf-8")
        self.assertIn('"qq": Platform.QQ', source)

    def test_qq_in_channel_directory(self):
        # channel_directory uses Platform enum iteration, not hardcoded strings.
        # Verify the iteration mechanism covers QQ.
        from gateway.config import Platform

        source = Path("gateway/channel_directory.py").read_text(encoding="utf-8")
        self.assertIn("for plat in Platform", source)
        self.assertIn(Platform.QQ, list(Platform))

    def test_qq_in_cronjob_deliver_description(self):
        source = Path("tools/cronjob_tools.py").read_text(encoding="utf-8")
        self.assertIn("qq", source.lower())

    def test_qq_platform_hint_exists(self):
        from agent.prompt_builder import PLATFORM_HINTS

        self.assertIn("qq", PLATFORM_HINTS)
        self.assertIn("QQ Bot", PLATFORM_HINTS["qq"])

    def test_qq_in_status_display(self):
        source = Path("hermes_cli/status.py").read_text(encoding="utf-8")
        self.assertIn('"QQ"', source)

    def test_qq_in_setup_wizard(self):
        source = Path("hermes_cli/gateway.py").read_text(encoding="utf-8")
        self.assertIn('"qq"', source)
        self.assertIn("QQ_APP_ID", source)
        self.assertIn("QQ_CLIENT_SECRET", source)


class TestQQAdapterInit(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_init",
        "QQ_CLIENT_SECRET": "secret_init",
    }, clear=True)
    def test_adapter_reads_config_from_env(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        self.assertEqual(adapter._app_id, "app_init")
        self.assertEqual(adapter._client_secret, "secret_init")

    def test_adapter_reads_config_from_extra(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        config = PlatformConfig(extra={
            "app_id": "extra_app",
            "client_secret": "extra_secret",
            "markdown_support": False,
            "group_policy": "disabled",
            "group_allowlist": ["g1"],
        })
        adapter = QQBotAdapter(config)
        self.assertEqual(adapter._app_id, "extra_app")
        self.assertFalse(adapter._markdown_support)
        self.assertEqual(adapter._group_policy, "disabled")
        self.assertEqual(adapter._group_allowlist, ["g1"])

    def test_adapter_default_values(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        self.assertTrue(adapter._markdown_support)
        self.assertEqual(adapter._group_policy, "open")
        self.assertEqual(adapter._group_allowlist, [])


class TestCheckRequirements(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app",
        "QQ_CLIENT_SECRET": "secret",
    }, clear=False)
    def test_requirements_met_with_env_and_deps(self):
        from gateway.platforms.qq import check_qq_requirements

        # This depends on whether httpx/websockets are installed
        result = check_qq_requirements()
        expected = _HAS_HTTPX and _HAS_WEBSOCKETS
        self.assertEqual(result, expected)

    @patch.dict(os.environ, {}, clear=True)
    def test_requirements_missing_env_vars(self):
        from gateway.platforms.qq import check_qq_requirements

        self.assertFalse(check_qq_requirements())


class TestTokenManager(unittest.TestCase):
    def test_token_manager_init(self):
        from gateway.platforms.qq import TokenManager

        tm = TokenManager("app1", "secret1")
        self.assertEqual(tm.app_id, "app1")
        self.assertIsNone(tm._access_token)

    @patch("httpx.AsyncClient")
    def test_token_refresh_and_cache(self, mock_client_cls):
        from gateway.platforms.qq import TokenManager

        tm = TokenManager("app1", "secret1")
        mock_client = AsyncMock()
        mock_response = Mock()
        mock_response.json.return_value = {"access_token": "tok_abc", "expires_in": 7200}
        mock_response.raise_for_status = Mock()
        mock_client.post = AsyncMock(return_value=mock_response)

        token = asyncio.run(tm.get_token(mock_client))
        self.assertEqual(token, "tok_abc")

        # Second call should use cache (no new HTTP request)
        token2 = asyncio.run(tm.get_token(mock_client))
        self.assertEqual(token2, "tok_abc")
        self.assertEqual(mock_client.post.await_count, 1)

    def test_clear_resets_state(self):
        from gateway.platforms.qq import TokenManager

        tm = TokenManager("app1", "secret1")
        tm._access_token = "old_token"
        tm._expires_at = time.time() + 3600
        tm.clear()
        self.assertIsNone(tm._access_token)
        self.assertEqual(tm._expires_at, 0)


class TestMessageDedup(unittest.TestCase):
    def test_first_message_not_duplicate(self):
        from gateway.platforms.qq import MessageDedup

        dedup = MessageDedup()
        self.assertFalse(dedup.is_duplicate("msg_1"))

    def test_same_message_is_duplicate(self):
        from gateway.platforms.qq import MessageDedup

        dedup = MessageDedup()
        dedup.is_duplicate("msg_1")
        self.assertTrue(dedup.is_duplicate("msg_1"))

    def test_expired_entries_evicted(self):
        from gateway.platforms.qq import MessageDedup

        dedup = MessageDedup(window=1)
        dedup.is_duplicate("msg_old")
        # Manually backdate
        dedup._seen["msg_old"] = time.time() - 10
        # Trigger eviction by exceeding max_size with new messages
        dedup._max_size = 2
        dedup.is_duplicate("msg_new1")
        dedup.is_duplicate("msg_new2")
        # "msg_old" should be evicted now
        self.assertFalse(dedup.is_duplicate("msg_old"))


class TestUploadDedupCache(unittest.TestCase):
    def test_cache_miss_returns_none(self):
        from gateway.platforms.qq import UploadDedupCache

        cache = UploadDedupCache()
        self.assertIsNone(cache.get("hash1", "users", "target1", 1))

    def test_cache_hit_returns_file_info(self):
        from gateway.platforms.qq import UploadDedupCache

        cache = UploadDedupCache()
        cache.put("hash1", "users", "target1", 1, "file_info_abc")
        self.assertEqual(cache.get("hash1", "users", "target1", 1), "file_info_abc")

    def test_expired_entry_returns_none(self):
        from gateway.platforms.qq import UploadDedupCache

        cache = UploadDedupCache(ttl=1)
        cache.put("hash1", "users", "target1", 1, "old_info")
        # Backdate
        key = "hash1:users:target1:1"
        cache._cache[key].expires_at = time.time() - 10
        self.assertIsNone(cache.get("hash1", "users", "target1", 1))

    def test_eviction_when_full(self):
        from gateway.platforms.qq import UploadDedupCache

        cache = UploadDedupCache(max_size=2)
        cache.put("h1", "users", "t1", 1, "info1")
        cache.put("h2", "users", "t2", 1, "info2")
        cache.put("h3", "users", "t3", 1, "info3")  # Should evict oldest
        self.assertEqual(len(cache._cache), 2)


class TestAudioConversion(unittest.TestCase):
    def test_pcm_to_wav_produces_valid_header(self):
        from gateway.platforms.qq import _pcm_to_wav

        pcm = b"\x00" * 48000  # 1 second of silence at 24kHz mono 16-bit
        wav = _pcm_to_wav(pcm, sample_rate=24000)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertIn(b"WAVE", wav)
        self.assertIn(b"data", wav)
        self.assertEqual(len(wav), 44 + len(pcm))

    def test_strip_amr_header(self):
        from gateway.platforms.qq import _strip_amr_header

        with_header = b"#!AMR\n" + b"\x00" * 100
        stripped = _strip_amr_header(with_header)
        self.assertEqual(len(stripped), 100)

    def test_strip_amr_header_no_header(self):
        from gateway.platforms.qq import _strip_amr_header

        data = b"\x00" * 100
        self.assertEqual(_strip_amr_header(data), data)

    def test_is_silk_data_detects_silk(self):
        from gateway.platforms.qq import _is_silk_data

        self.assertTrue(_is_silk_data(b"\x02" + b"\x00" * 100))
        self.assertTrue(_is_silk_data(b"#!SILK_V3" + b"\x00" * 100))
        self.assertFalse(_is_silk_data(b"RIFF" + b"\x00" * 100))

    def test_detect_audio_format(self):
        from gateway.platforms.qq import _detect_audio_format

        self.assertEqual(_detect_audio_format(".wav"), "wav")
        self.assertEqual(_detect_audio_format(".mp3"), "mp3")
        self.assertEqual(_detect_audio_format(".silk"), "silk")
        self.assertEqual(_detect_audio_format(".ogg"), "wav")  # default


class TestFileHashing(unittest.TestCase):
    def test_compute_md5(self):
        from gateway.platforms.qq import _compute_md5

        data = b"hello world"
        result = _compute_md5(data)
        self.assertEqual(len(result), 32)
        self.assertIsInstance(result, str)

    def test_compute_file_md5(self):
        from gateway.platforms.qq import _compute_file_md5

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"test content")
            path = f.name
        try:
            result = _compute_file_md5(path)
            self.assertEqual(len(result), 32)
        finally:
            os.unlink(path)

    def test_compute_streaming_hashes(self):
        from gateway.platforms.qq import _compute_streaming_hashes

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"a" * 100)
            path = f.name
        try:
            md5, sha1, md5_10m = _compute_streaming_hashes(path)
            self.assertEqual(len(md5), 32)
            self.assertEqual(len(sha1), 40)
            self.assertEqual(len(md5_10m), 32)
        finally:
            os.unlink(path)


class TestGroupPolicy(unittest.TestCase):
    def test_open_policy_accepts_all(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig(extra={"group_policy": "open"}))
        self.assertTrue(adapter._should_accept_group_message("any_group", {}))

    def test_disabled_policy_rejects_all(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig(extra={"group_policy": "disabled"}))
        self.assertFalse(adapter._should_accept_group_message("any_group", {}))

    def test_allowlist_policy_accepts_only_listed(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig(extra={
            "group_policy": "allowlist",
            "group_allowlist": ["group_a", "group_b"],
        }))
        self.assertTrue(adapter._should_accept_group_message("group_a", {}))
        self.assertFalse(adapter._should_accept_group_message("group_c", {}))


class TestMessageParsing(unittest.TestCase):
    def test_parse_timestamp_valid(self):
        from gateway.platforms.qq import QQBotAdapter

        ts = QQBotAdapter._parse_timestamp("2024-06-15T10:30:00+08:00")
        self.assertEqual(ts.year, 2024)
        self.assertEqual(ts.month, 6)
        self.assertEqual(ts.hour, 10)

    def test_parse_timestamp_none_returns_now(self):
        from gateway.platforms.qq import QQBotAdapter

        ts = QQBotAdapter._parse_timestamp(None)
        self.assertIsNotNone(ts)

    def test_parse_timestamp_invalid_returns_now(self):
        from gateway.platforms.qq import QQBotAdapter

        ts = QQBotAdapter._parse_timestamp("not-a-date")
        self.assertIsNotNone(ts)

    def test_detect_message_type_voice(self):
        from gateway.platforms.qq import QQBotAdapter

        self.assertEqual(
            QQBotAdapter._detect_message_type({}, ["audio"]),
            MessageType.VOICE,
        )

    def test_detect_message_type_photo(self):
        from gateway.platforms.qq import QQBotAdapter

        self.assertEqual(
            QQBotAdapter._detect_message_type({}, ["image"]),
            MessageType.PHOTO,
        )

    def test_detect_message_type_text(self):
        from gateway.platforms.qq import QQBotAdapter

        from gateway.platforms.base import MessageType
        self.assertEqual(
            QQBotAdapter._detect_message_type({"content": "hello"}, []),
            MessageType.TEXT,
        )

    def test_detect_message_type_command(self):
        from gateway.platforms.qq import QQBotAdapter

        self.assertEqual(
            QQBotAdapter._detect_message_type({"content": "/help"}, []),
            MessageType.COMMAND,
        )


class TestAdapterSend(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_send",
        "QQ_CLIENT_SECRET": "secret_send",
    }, clear=True)
    def test_send_builds_correct_c2c_endpoint(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        endpoint = adapter._build_send_endpoint("user_openid_123", is_group=False)
        self.assertIn("/v2/users/user_openid_123/messages", endpoint)

    def test_send_builds_correct_group_endpoint(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        endpoint = adapter._build_send_endpoint("group_openid_456", is_group=True)
        self.assertIn("/v2/groups/group_openid_456/messages", endpoint)

    def test_send_not_connected_returns_error(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        result = asyncio.run(adapter.send("chat_1", "hello"))
        self.assertFalse(result.success)
        self.assertIn("Not connected", result.error)

    def test_make_msg_seq_in_range(self):
        from gateway.platforms.qq import QQBotAdapter

        for _ in range(100):
            seq = QQBotAdapter._make_msg_seq()
            self.assertGreaterEqual(seq, 0)
            self.assertLess(seq, 65536)


class TestFormatMessage(unittest.TestCase):
    def test_markdown_mode_preserves_content(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig(extra={"markdown_support": True}))
        result = adapter.format_message("**bold** and *italic*")
        self.assertEqual(result, "**bold** and *italic*")

    def test_plain_text_mode_strips_markdown(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig(extra={"markdown_support": False}))
        result = adapter.format_message("**bold** and *italic* and `code`")
        self.assertNotIn("**", result)
        self.assertNotIn("*", result)
        self.assertNotIn("`", result)
        self.assertIn("bold", result)
        self.assertIn("italic", result)


class TestGetChatInfo(unittest.TestCase):
    def test_get_chat_info_returns_basic_info(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        info = asyncio.run(adapter.get_chat_info("group_openid_123"))
        self.assertEqual(info["chat_id"], "group_openid_123")
        self.assertIn("type", info)


class TestWebSocketDispatch(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_ws",
        "QQ_CLIENT_SECRET": "secret_ws",
    }, clear=True)
    def test_on_dispatch_ready_sets_session(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        ready_data = {
            "session_id": "sess_abc",
            "user": {"id": "bot_user_1"},
        }
        asyncio.run(adapter._on_dispatch("READY", ready_data))
        self.assertEqual(adapter._session_id, "sess_abc")
        self.assertEqual(adapter._user_id, "bot_user_1")

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_ws",
        "QQ_CLIENT_SECRET": "secret_ws",
    }, clear=True)
    def test_on_dispatch_tracks_seq(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        asyncio.run(adapter._on_dispatch("C2C_MESSAGE_CREATE", {"s": 42}))
        self.assertEqual(adapter._seq, 42)

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_ws",
        "QQ_CLIENT_SECRET": "secret_ws",
    }, clear=True)
    def test_on_ws_message_handles_hello(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        adapter._send_identify = AsyncMock()
        adapter._start_heartbeat = Mock()

        asyncio.run(adapter._on_ws_message({"op": 10, "d": {"heartbeat_interval": 41250}}))

        adapter._send_identify.assert_awaited_once()
        adapter._start_heartbeat.assert_called_once()

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_ws",
        "QQ_CLIENT_SECRET": "secret_ws",
    }, clear=True)
    def test_on_ws_message_handles_reconnect(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        mock_ws = AsyncMock()
        adapter._ws = mock_ws

        asyncio.run(adapter._on_ws_message({"op": 7}))
        mock_ws.close.assert_awaited_once()


class TestUploadEndpoint(unittest.TestCase):
    def test_c2c_upload_endpoint(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        endpoint = adapter._build_upload_endpoint("user_123", is_group=False)
        self.assertIn("/v2/users/user_123/files", endpoint)

    def test_group_upload_endpoint(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        endpoint = adapter._build_upload_endpoint("group_456", is_group=True)
        self.assertIn("/v2/groups/group_456/files", endpoint)


class TestSlashCommands(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_cmd",
        "QQ_CLIENT_SECRET": "secret_cmd",
    }, clear=True)
    def test_bot_ping_recognized(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))

        result = asyncio.run(
            adapter._handle_slash_command("/bot-ping", "chat_1", "msg_1")
        )
        # Returns "" (empty string) to indicate command was handled
        self.assertEqual(result, "")
        adapter.send.assert_awaited()

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_cmd",
        "QQ_CLIENT_SECRET": "secret_cmd",
    }, clear=True)
    def test_bot_version_recognized(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))

        result = asyncio.run(
            adapter._handle_slash_command("/bot-version", "chat_1", "msg_1")
        )
        self.assertEqual(result, "")

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_cmd",
        "QQ_CLIENT_SECRET": "secret_cmd",
    }, clear=True)
    def test_unknown_command_returns_none(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        result = asyncio.run(
            adapter._handle_slash_command("/unknown", "chat_1", "msg_1")
        )
        self.assertIsNone(result)


class TestInboundC2C(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_in",
        "QQ_CLIENT_SECRET": "secret_in",
    }, clear=True)
    def test_c2c_message_dispatches_to_handler(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        adapter.handle_message = AsyncMock()
        adapter._handle_slash_command = AsyncMock(return_value=None)

        data = {
            "id": "msg_c2c_1",
            "author": {"user_openid": "user_abc"},
            "content": "hello QQ",
            "timestamp": "2024-06-15T10:30:00+08:00",
            "attachments": [],
        }

        asyncio.run(adapter._on_c2c_message(data))
        adapter.handle_message.assert_awaited_once()
        event = adapter.handle_message.await_args.args[0]
        self.assertEqual(event.text, "hello QQ")
        self.assertEqual(event.source.chat_id, "user_abc")
        self.assertEqual(event.source.chat_type, "dm")

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_in",
        "QQ_CLIENT_SECRET": "secret_in",
    }, clear=True)
    def test_duplicate_c2c_message_skipped(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        adapter.handle_message = AsyncMock()

        data = {
            "id": "msg_dup",
            "author": {"user_openid": "user_abc"},
            "content": "dup",
            "attachments": [],
        }

        asyncio.run(adapter._on_c2c_message(data))
        asyncio.run(adapter._on_c2c_message(data))  # Second time = duplicate
        self.assertEqual(adapter.handle_message.await_count, 1)


class TestInboundGroup(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_in",
        "QQ_CLIENT_SECRET": "secret_in",
    }, clear=True)
    def test_group_message_strips_at_mention(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig(extra={"group_policy": "open"}))
        adapter.handle_message = AsyncMock()
        adapter._handle_slash_command = AsyncMock(return_value=None)

        data = {
            "id": "msg_grp_1",
            "group_openid": "group_xyz",
            "author": {"member_openid": "member_abc"},
            "content": "@bot_user hello group",
            "timestamp": "2024-06-15T10:30:00+08:00",
            "attachments": [],
        }

        asyncio.run(adapter._on_group_message(data))
        event = adapter.handle_message.await_args.args[0]
        self.assertNotIn("@bot_user", event.text)
        self.assertEqual(event.text, "hello group")
        self.assertEqual(event.source.chat_type, "group")

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_in",
        "QQ_CLIENT_SECRET": "secret_in",
    }, clear=True)
    def test_group_message_rejected_by_policy(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig(extra={
            "group_policy": "allowlist",
            "group_allowlist": ["group_allowed"],
        }))
        adapter.handle_message = AsyncMock()

        data = {
            "id": "msg_grp_blocked",
            "group_openid": "group_blocked",
            "author": {"member_openid": "member_abc"},
            "content": "hello",
            "attachments": [],
        }

        asyncio.run(adapter._on_group_message(data))
        adapter.handle_message.assert_not_awaited()


class TestTypingIndicator(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_type",
        "QQ_CLIENT_SECRET": "secret_type",
    }, clear=True)
    def test_send_typing_skips_group(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        # Should not raise for group
        asyncio.run(adapter.send_typing("group_1", metadata={"is_group": True}))

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_type",
        "QQ_CLIENT_SECRET": "secret_type",
    }, clear=True)
    def test_send_typing_no_client(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        # Should not raise when not connected
        asyncio.run(adapter.send_typing("user_1"))


class TestConstants(unittest.TestCase):
    def test_qq_file_types(self):
        from gateway.platforms.qq import QQFileType

        self.assertEqual(QQFileType.IMAGE, 1)
        self.assertEqual(QQFileType.VIDEO, 2)
        self.assertEqual(QQFileType.VOICE, 3)
        self.assertEqual(QQFileType.FILE, 4)

    def test_adapter_version_defined(self):
        from gateway.platforms.qq import ADAPTER_VERSION

        self.assertIsInstance(ADAPTER_VERSION, str)
        self.assertRegex(ADAPTER_VERSION, r"\d+\.\d+\.\d+")

    def test_max_message_lengths(self):
        from gateway.platforms.qq import MAX_TEXT_LENGTH, MAX_MARKDOWN_LENGTH

        self.assertEqual(MAX_TEXT_LENGTH, 2000)
        self.assertEqual(MAX_MARKDOWN_LENGTH, 8000)


# Import MessageType for tests that need it
from gateway.platforms.base import MessageType


class TestSendImage(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_img",
        "QQ_CLIENT_SECRET": "secret_img",
    }, clear=True)
    def test_send_image_url_success(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        adapter._http_client = AsyncMock()
        adapter._token_manager = Mock()
        adapter._token_manager.get_token = AsyncMock(return_value="tok")

        # Mock upload: returns file_info
        upload_resp = Mock()
        upload_resp.status_code = 200
        upload_resp.raise_for_status = Mock()
        upload_resp.json.return_value = {"file_info": "fi_img_123"}

        # Mock send: returns message id
        send_resp = Mock()
        send_resp.status_code = 200
        send_resp.json.return_value = {"id": "msg_img_1"}

        adapter._http_client.post = AsyncMock(side_effect=[upload_resp, send_resp])

        result = asyncio.run(adapter.send_image(
            "user_1", "https://example.com/img.jpg",
            caption="test image",
            metadata={"is_group": False},
        ))
        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "msg_img_1")

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_img",
        "QQ_CLIENT_SECRET": "secret_img",
    }, clear=True)
    def test_send_image_upload_failure(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        adapter._http_client = AsyncMock()
        adapter._token_manager = Mock()
        adapter._token_manager.get_token = AsyncMock(return_value="tok")
        adapter._upload_media_url = AsyncMock(return_value=None)

        result = asyncio.run(adapter.send_image(
            "user_1", "https://example.com/bad.jpg",
        ))
        self.assertFalse(result.success)


class TestSendDocument(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_doc",
        "QQ_CLIENT_SECRET": "secret_doc",
    }, clear=True)
    def test_send_small_document(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        adapter._http_client = AsyncMock()
        adapter._token_manager = Mock()
        adapter._token_manager.get_token = AsyncMock(return_value="tok")

        # Mock upload
        upload_resp = Mock()
        upload_resp.status_code = 200
        upload_resp.raise_for_status = Mock()
        upload_resp.json.return_value = {"file_info": "fi_doc_1"}

        # Mock send
        send_resp = Mock()
        send_resp.status_code = 200
        send_resp.json.return_value = {"id": "msg_doc_1"}

        adapter._http_client.post = AsyncMock(side_effect=[upload_resp, send_resp])

        # Create temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"test document content")
            path = f.name
        try:
            result = asyncio.run(adapter.send_document(
                "user_1", path, caption="test doc",
                metadata={"is_group": False},
            ))
            self.assertTrue(result.success)
        finally:
            os.unlink(path)

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_doc",
        "QQ_CLIENT_SECRET": "secret_doc",
    }, clear=True)
    def test_send_large_document_uses_chunked(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        adapter._chunked_upload = AsyncMock(return_value="fi_chunked")
        adapter._send_media_message = AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="msg_chunk")
        )

        # Create a file larger than CHUNKED_UPLOAD_THRESHOLD
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(b"\x00" * (26 * 1024 * 1024))  # 26MB
            path = f.name
        try:
            # Patch os.path.getsize to return large size
            with patch("os.path.getsize", return_value=26 * 1024 * 1024):
                result = asyncio.run(adapter.send_document(
                    "user_1", path, metadata={"is_group": False},
                ))
                adapter._chunked_upload.assert_awaited_once()
        finally:
            os.unlink(path)


class TestSendVoice(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_voice",
        "QQ_CLIENT_SECRET": "secret_voice",
    }, clear=True)
    def test_send_voice_wav_file(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        adapter._http_client = AsyncMock()
        adapter._token_manager = Mock()
        adapter._token_manager.get_token = AsyncMock(return_value="tok")

        # Mock upload
        upload_resp = Mock()
        upload_resp.status_code = 200
        upload_resp.raise_for_status = Mock()
        upload_resp.json.return_value = {"file_info": "fi_voice_1"}

        # Mock send
        send_resp = Mock()
        send_resp.status_code = 200
        send_resp.json.return_value = {"id": "msg_voice_1"}

        adapter._http_client.post = AsyncMock(side_effect=[upload_resp, send_resp])

        # Create temp WAV file
        pcm = b"\x00" * 48000
        wav_header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', 36 + len(pcm), b'WAVE', b'fmt ',
            16, 1, 1, 24000, 48000, 2, 16,
            b'data', len(pcm),
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
            f.write(wav_header + pcm)
            path = f.name
        try:
            result = asyncio.run(adapter.send_voice(
                "user_1", path, metadata={"is_group": False},
            ))
            self.assertTrue(result.success)
        finally:
            os.unlink(path)


class TestExtractContent(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_ext",
        "QQ_CLIENT_SECRET": "secret_ext",
    }, clear=True)
    def test_extract_text_only(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        data = {
            "content": "hello world",
            "attachments": [],
        }
        text, urls, types = asyncio.run(adapter._extract_content(data))
        self.assertEqual(text, "hello world")
        self.assertEqual(urls, [])
        self.assertEqual(types, [])

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_ext",
        "QQ_CLIENT_SECRET": "secret_ext",
    }, clear=True)
    @patch("gateway.platforms.qq.cache_image_from_url", new_callable=AsyncMock)
    def test_extract_image_attachment(self, mock_cache_img):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        mock_cache_img.return_value = "/tmp/cached_img.jpg"
        adapter = QQBotAdapter(PlatformConfig())
        data = {
            "content": "see this",
            "attachments": [{
                "content_type": "image/png",
                "url": "https://example.com/img.png",
                "filename": "screenshot.png",
            }],
        }
        text, urls, types = asyncio.run(adapter._extract_content(data))
        self.assertIn("image", types)
        self.assertEqual(len(urls), 1)

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_ext",
        "QQ_CLIENT_SECRET": "secret_ext",
    }, clear=True)
    def test_extract_empty_message(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        data = {"content": "", "attachments": []}
        text, urls, types = asyncio.run(adapter._extract_content(data))
        self.assertEqual(text, "")
        self.assertEqual(urls, [])
        self.assertEqual(types, [])


class TestChunkedUploadParams(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_chunk",
        "QQ_CLIENT_SECRET": "secret_chunk",
    }, clear=True)
    def test_prepare_uses_correct_param_names(self):
        """Verify upload_prepare sends md5/sha1/md5_10m (not file_hash etc)."""
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter, QQ_API_BASE

        adapter = QQBotAdapter(PlatformConfig())
        adapter._http_client = AsyncMock()
        adapter._token_manager = Mock()
        adapter._token_manager.get_token = AsyncMock(return_value="tok")

        # Create a temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(b"\x00" * 100)
            path = f.name

        # Mock prepare response with no parts (instant upload)
        prepare_resp = Mock()
        prepare_resp.status_code = 200
        prepare_resp.raise_for_status = Mock()
        prepare_resp.json.return_value = {
            "upload_id": "uid",
            "upload_info": [],  # Empty = instant upload
            "file_info": "fi_instant",
        }
        adapter._http_client.post = AsyncMock(return_value=prepare_resp)

        try:
            result = asyncio.run(adapter._chunked_upload(path, "user_1", False))
            self.assertEqual(result, "fi_instant")

            # Verify the payload sent to upload_prepare
            call_args = adapter._http_client.post.await_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            self.assertIn("md5", payload)
            self.assertIn("sha1", payload)
            self.assertIn("md5_10m", payload)
            self.assertIn("file_type", payload)
            self.assertNotIn("file_hash", payload)
            self.assertNotIn("file_sha1", payload)
            self.assertNotIn("file_10m_md5", payload)
        finally:
            os.unlink(path)

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_chunk",
        "QQ_CLIENT_SECRET": "secret_chunk",
    }, clear=True)
    def test_full_chunked_flow(self):
        """Verify the 3-step chunked upload flow."""
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        adapter._http_client = AsyncMock()
        adapter._token_manager = Mock()
        adapter._token_manager.get_token = AsyncMock(return_value="tok")
        adapter._put_to_presigned = AsyncMock()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
            f.write(b"\x00" * 100)
            path = f.name

        # Prepare response
        prepare_resp = Mock()
        prepare_resp.status_code = 200
        prepare_resp.raise_for_status = Mock()
        prepare_resp.json.return_value = {
            "upload_id": "uid_123",
            "upload_info": [{"index": 1, "presigned_url": "https://presigned.example.com/p1"}],
            "block_size": 4194304,
        }

        # Part finish response
        part_finish_resp = Mock()
        part_finish_resp.status_code = 200

        # Complete upload response
        complete_resp = Mock()
        complete_resp.status_code = 200
        complete_resp.raise_for_status = Mock()
        complete_resp.json.return_value = {"file_info": "fi_completed"}

        adapter._http_client.post = AsyncMock(
            side_effect=[prepare_resp, part_finish_resp, complete_resp]
        )

        try:
            result = asyncio.run(adapter._chunked_upload(path, "user_1", False))
            self.assertEqual(result, "fi_completed")

            # Verify 3 HTTP calls: prepare, part_finish, complete
            self.assertEqual(adapter._http_client.post.await_count, 3)

            # Verify part_finish payload
            part_finish_call = adapter._http_client.post.await_args_list[1]
            pf_payload = part_finish_call.kwargs.get("json") or part_finish_call[1].get("json")
            self.assertEqual(pf_payload["upload_id"], "uid_123")
            self.assertEqual(pf_payload["part_index"], 1)
            self.assertIn("md5", pf_payload)
            self.assertIn("block_size", pf_payload)

            # Verify complete payload
            complete_call = adapter._http_client.post.await_args_list[2]
            c_payload = complete_call.kwargs.get("json") or complete_call[1].get("json")
            self.assertEqual(c_payload["upload_id"], "uid_123")
        finally:
            os.unlink(path)


class TestDownloadUrlSSRF(unittest.TestCase):
    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_ssrf",
        "QQ_CLIENT_SECRET": "secret_ssrf",
    }, clear=True)
    def test_download_blocks_unsafe_url(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        with patch("tools.url_safety.is_safe_url", return_value=False):
            with self.assertRaises(ValueError):
                asyncio.run(adapter._download_url("http://169.254.169.254/metadata"))

    @patch.dict(os.environ, {
        "QQ_APP_ID": "app_ssrf",
        "QQ_CLIENT_SECRET": "secret_ssrf",
    }, clear=True)
    def test_download_allows_safe_url(self):
        from gateway.config import PlatformConfig
        from gateway.platforms.qq import QQBotAdapter

        adapter = QQBotAdapter(PlatformConfig())
        mock_resp = Mock()
        mock_resp.status_code = 200
        mock_resp.content = b"image_data"
        mock_resp.raise_for_status = Mock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("tools.url_safety.is_safe_url", return_value=True):
            with patch("httpx.AsyncClient", return_value=mock_client):
                data = asyncio.run(adapter._download_url("https://example.com/img.jpg"))
                self.assertEqual(data, b"image_data")


class TestCronDeliveryPlatforms(unittest.TestCase):
    def test_qq_in_known_delivery_platforms(self):
        """Verify 'qq' is in the cron delivery platform whitelist."""
        from cron.scheduler import _KNOWN_DELIVERY_PLATFORMS
        self.assertIn("qq", _KNOWN_DELIVERY_PLATFORMS)


if __name__ == "__main__":
    unittest.main()
