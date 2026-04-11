"""
QQ Bot platform adapter for the Hermes gateway.

Connects to QQ Bot API v2 via WebSocket for real-time message reception
and uses REST API for message sending, media upload, and typing indicators.

Supports:
    - C2C (private) and group messaging
    - Image, voice, video, file attachments (inbound + outbound)
    - Markdown messages (configurable)
    - Multi-account support
    - Group policy (open / allowlist / disabled)
    - Slash commands (/bot-ping, /bot-version, /bot-help)
    - Chunked upload for large files
    - SILK/WAV/MP3 audio conversion
    - Upload dedup cache
    - Message dedup with TTL eviction

Requires:
    pip install websockets httpx

Configuration (config.yaml or env vars):
    platforms:
      qq:
        enabled: true
        extra:
          app_id: "YOUR_APP_ID"            # or QQ_APP_ID env var
          client_secret: "YOUR_SECRET"     # or QQ_CLIENT_SECRET env var
          markdown_support: true           # default: true
          group_policy: "open"             # open | allowlist | disabled
          group_allowlist: []              # group_openid list
"""

import asyncio
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    cache_image_from_url,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QQ_API_BASE = "https://api.sgroup.qq.com"
QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_GATEWAY_URL = "https://api.sgroup.qq.com/gateway"

MAX_TEXT_LENGTH = 2000       # QQ text message limit (plain text)
MAX_MARKDOWN_LENGTH = 8000  # QQ markdown message limit
MAX_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB per chunk for chunked upload
CHUNKED_UPLOAD_THRESHOLD = 25 * 1024 * 1024  # Files >25MB use chunked upload
UPLOAD_CACHE_MAX = 500
UPLOAD_CACHE_TTL = 3600      # 1 hour
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 1000
TOKEN_REFRESH_MARGIN = 300   # Refresh token 5 minutes before expiry
HEARTBEAT_INTERVAL_DEFAULT = 41.25  # seconds, fallback if not in READY

RECONNECT_BACKOFF = [2, 5, 10, 30, 60]

# QQ media file types
class QQFileType:
    IMAGE = 1
    VIDEO = 2
    VOICE = 3
    FILE = 4

# QQ message types
MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2
MSG_TYPE_MEDIA = 7
MSG_TYPE_INPUT_NOTIFY = 6

# Audio conversion constants
SILK_SAMPLE_RATE = 24000
AUDIO_NATIVE_FORMATS = {".wav", ".mp3", ".silk"}

# Slash commands
SLASH_COMMANDS = {
    "/bot-ping": "Measure bot latency",
    "/bot-version": "Show adapter version info",
    "/bot-help": "List available commands",
}

ADAPTER_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Utility: Audio conversion
# ---------------------------------------------------------------------------

def _pcm_to_wav(pcm_data: bytes, sample_rate: int = SILK_SAMPLE_RATE,
                channels: int = 1, sample_width: int = 2) -> bytes:
    """Convert raw PCM bytes to WAV format with proper header."""
    num_samples = len(pcm_data) // (channels * sample_width)
    data_size = len(pcm_data)
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        36 + data_size,
        b'WAVE',
        b'fmt ',
        16,  # chunk size
        1,   # PCM format
        channels,
        sample_rate,
        sample_rate * channels * sample_width,  # byte rate
        channels * sample_width,  # block align
        sample_width * 8,  # bits per sample
        b'data',
        data_size,
    )
    return header + pcm_data


def _strip_amr_header(data: bytes) -> bytes:
    """Remove AMR header bytes (#!AMR\\n) from audio data."""
    if data[:6] == b"#!AMR\n":
        return data[6:]
    return data


def _is_silk_data(data: bytes) -> bool:
    """Check if audio data starts with SILK codec signature."""
    return data[:1] == b"\x02" or data[:9] == b"#!SILK_V3"


async def convert_silk_to_wav(silk_data: bytes) -> bytes:
    """Convert SILK audio data to WAV format for STT processing.

    Tries ffmpeg first (most reliable), falls back to raw PCM passthrough.
    """
    # Try ffmpeg with silk codec
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-f", "silk", "-i", "pipe:0",
            "-f", "wav", "pipe:1",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=silk_data), timeout=30
        )
        if proc.returncode == 0 and stdout:
            return stdout
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        pass

    # Try ffmpeg as raw PCM (SILK decoded to s16le at 24kHz)
    try:
        # Assume SILK decodes to PCM s16le at 24kHz mono
        pcm = _strip_amr_header(silk_data)
        return _pcm_to_wav(pcm, sample_rate=SILK_SAMPLE_RATE)
    except Exception:
        pass

    # Last resort: wrap raw bytes as WAV
    return _pcm_to_wav(silk_data, sample_rate=SILK_SAMPLE_RATE)


async def convert_audio_to_silk(audio_path: str) -> Optional[bytes]:
    """Convert an audio file to SILK format for QQ voice upload.

    Uses ffmpeg to decode input → raw PCM at 24kHz mono s16le,
    then encodes to SILK if possible, otherwise returns WAV.
    """
    # Step 1: Decode to PCM via ffmpeg
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", audio_path,
            "-f", "s16le", "-ar", str(SILK_SAMPLE_RATE),
            "-ac", "1", "-acodec", "pcm_s16le",
            "pipe:1",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=b""), timeout=60
        )
        if proc.returncode != 0 or not stdout:
            logger.warning("ffmpeg PCM decode failed for %s", audio_path)
            return None
        pcm_data = stdout
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as e:
        logger.warning("ffmpeg not available for audio conversion: %s", e)
        return None

    # Step 2: Encode PCM to SILK via ffmpeg
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-f", "s16le", "-ar", str(SILK_SAMPLE_RATE),
            "-ac", "1", "-i", "pipe:0",
            "-f", "silk", "pipe:1",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=pcm_data), timeout=60
        )
        if proc.returncode == 0 and stdout:
            return stdout
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        pass

    # Step 3: If SILK encoding unavailable, return WAV (QQ accepts WAV natively)
    return _pcm_to_wav(pcm_data, sample_rate=SILK_SAMPLE_RATE)


def _detect_audio_format(ext: str) -> str:
    """Map file extension to QQ-compatible audio format."""
    ext = ext.lower()
    if ext in (".wav",):
        return "wav"
    if ext in (".mp3",):
        return "mp3"
    if ext in (".silk",):
        return "silk"
    return "wav"  # default to WAV


# ---------------------------------------------------------------------------
# Utility: File hashing
# ---------------------------------------------------------------------------

def _compute_md5(data: bytes) -> str:
    """Compute MD5 hex digest of data."""
    return hashlib.md5(data).hexdigest()


def _compute_file_md5(path: str) -> str:
    """Compute MD5 hex digest of a file (streaming)."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def _compute_streaming_hashes(path: str) -> Tuple[str, str, str]:
    """Compute md5, sha1, and md5_of_first_10mb for chunked upload."""
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    md5_10m = hashlib.md5()
    ten_mb = 10 * 1024 * 1024
    total_read = 0

    with open(path, "rb") as f:
        while chunk := f.read(8192):
            md5.update(chunk)
            sha1.update(chunk)
            total_read += len(chunk)
            if total_read <= ten_mb:
                md5_10m.update(chunk)
            elif total_read - len(chunk) < ten_mb:
                # This chunk straddles the 10MB boundary
                remaining = ten_mb - (total_read - len(chunk))
                md5_10m.update(chunk[:remaining])

    return md5.hexdigest(), sha1.hexdigest(), md5_10m.hexdigest()


# ---------------------------------------------------------------------------
# Upload dedup cache
# ---------------------------------------------------------------------------

@dataclass
class _UploadCacheEntry:
    file_info: str
    expires_at: float


class UploadDedupCache:
    """In-memory cache mapping content hash → QQ file_info to avoid re-uploads.

    Key format: ``{content_hash}:{scope}:{target_id}:{file_type}``
    """

    def __init__(self, max_size: int = UPLOAD_CACHE_MAX,
                 ttl: int = UPLOAD_CACHE_TTL) -> None:
        self._cache: Dict[str, _UploadCacheEntry] = {}
        self._max_size = max_size
        self._ttl = ttl

    def _make_key(self, content_hash: str, scope: str,
                  target_id: str, file_type: int) -> str:
        return f"{content_hash}:{scope}:{target_id}:{file_type}"

    def get(self, content_hash: str, scope: str,
            target_id: str, file_type: int) -> Optional[str]:
        key = self._make_key(content_hash, scope, target_id, file_type)
        entry = self._cache.get(key)
        if entry and entry.expires_at > time.time():
            return entry.file_info
        if entry:
            del self._cache[key]
        return None

    def put(self, content_hash: str, scope: str,
            target_id: str, file_type: int, file_info: str) -> None:
        if len(self._cache) >= self._max_size:
            self._evict()
        key = self._make_key(content_hash, scope, target_id, file_type)
        self._cache[key] = _UploadCacheEntry(
            file_info=file_info,
            expires_at=time.time() + self._ttl,
        )

    def _evict(self) -> None:
        now = time.time()
        expired = [k for k, v in self._cache.items() if v.expires_at <= now]
        for k in expired:
            del self._cache[k]
        if len(self._cache) >= self._max_size:
            oldest_key = min(self._cache, key=lambda k: self._cache[k].expires_at)
            del self._cache[oldest_key]


# ---------------------------------------------------------------------------
# Message dedup
# ---------------------------------------------------------------------------

class MessageDedup:
    """Track seen message IDs with TTL-based eviction."""

    def __init__(self, window: int = DEDUP_WINDOW_SECONDS,
                 max_size: int = DEDUP_MAX_SIZE) -> None:
        self._seen: Dict[str, float] = {}
        self._window = window
        self._max_size = max_size

    def is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen) > self._max_size:
            cutoff = now - self._window
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}

        if msg_id in self._seen:
            return True
        self._seen[msg_id] = now
        return False


# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

class TokenManager:
    """Manage QQ Bot access tokens with caching and background refresh.

    One TokenManager per appId. Supports singleflight to prevent
    concurrent token requests for the same appId.
    """

    def __init__(self, app_id: str, client_secret: str) -> None:
        self._app_id = app_id
        self._client_secret = client_secret
        self._access_token: Optional[str] = None
        self._expires_at: float = 0
        self._refresh_task: Optional[asyncio.Task] = None
        self._singleflight_lock = asyncio.Lock()

    @property
    def app_id(self) -> str:
        return self._app_id

    async def get_token(self, client: "httpx.AsyncClient") -> str:
        """Get a valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._expires_at - TOKEN_REFRESH_MARGIN:
            return self._access_token

        async with self._singleflight_lock:
            # Double-check after acquiring lock
            if self._access_token and time.time() < self._expires_at - TOKEN_REFRESH_MARGIN:
                return self._access_token
            await self._refresh_token(client)

        if not self._access_token:
            raise RuntimeError(f"Failed to obtain access token for appId={self._app_id}")
        return self._access_token

    async def _refresh_token(self, client: "httpx.AsyncClient") -> None:
        """Fetch a new access token from QQ API."""
        payload = {
            "appId": self._app_id,
            "clientSecret": self._client_secret,
        }
        try:
            resp = await client.post(QQ_TOKEN_URL, json=payload, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            expires_in = int(data.get("expires_in", 7200))
            self._expires_at = time.time() + expires_in
            logger.debug("[QQ] Token refreshed for appId=%s, expires_in=%ds",
                         self._app_id, expires_in)
        except Exception as e:
            logger.error("[QQ] Token refresh failed for appId=%s: %s", self._app_id, e)
            raise

    def start_background_refresh(self, client: "httpx.AsyncClient") -> None:
        """Start background token refresh task."""
        if self._refresh_task and not self._refresh_task.done():
            return

        async def _refresh_loop() -> None:
            while True:
                try:
                    ttl = self._expires_at - time.time() - TOKEN_REFRESH_MARGIN
                    wait = max(ttl, 60)
                    await asyncio.sleep(wait)
                    await self.get_token(client)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.warning("[QQ] Background token refresh error: %s", e)
                    await asyncio.sleep(60)

        self._refresh_task = asyncio.create_task(_refresh_loop())

    def stop_background_refresh(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()

    def clear(self) -> None:
        self.stop_background_refresh()
        self._access_token = None
        self._expires_at = 0


# ---------------------------------------------------------------------------
# Check requirements
# ---------------------------------------------------------------------------

def check_qq_requirements() -> bool:
    """Check if QQ Bot dependencies are available."""
    if not HTTPX_AVAILABLE or not WEBSOCKETS_AVAILABLE:
        return False
    if not os.getenv("QQ_APP_ID") or not os.getenv("QQ_CLIENT_SECRET"):
        return False
    return True


# ---------------------------------------------------------------------------
# QQ Bot Adapter
# ---------------------------------------------------------------------------

class QQBotAdapter(BasePlatformAdapter):
    """QQ Bot adapter connecting via WebSocket + REST API v2.

    Supports C2C and group messaging with media attachments.
    """

    MAX_MESSAGE_LENGTH = MAX_TEXT_LENGTH

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform.QQ)

        extra = config.extra or {}

        # Multi-account support: primary account from config or env
        self._app_id: str = extra.get("app_id") or os.getenv("QQ_APP_ID", "")
        self._client_secret: str = (
            extra.get("client_secret") or os.getenv("QQ_CLIENT_SECRET", "")
        )

        # Feature flags
        self._markdown_support: bool = extra.get("markdown_support", True)

        # Group policy
        self._group_policy: str = extra.get("group_policy", "open")
        self._group_allowlist: List[str] = extra.get("group_allowlist", [])

        # Voice direct upload formats
        self._voice_direct_formats: List[str] = extra.get(
            "voice_direct_upload_formats", [".wav", ".mp3", ".silk"]
        )

        # Connection state
        self._ws: Optional[Any] = None
        self._ws_url: Optional[str] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._session_id: Optional[str] = None
        self._seq: Optional[int] = None
        self._user_id: Optional[str] = None  # Bot's own user ID

        # HTTP client
        self._http_client: Optional["httpx.AsyncClient"] = None

        # Token manager (one per appId)
        self._token_manager: Optional[TokenManager] = None

        # Dedup
        self._dedup = MessageDedup()

        # Upload cache
        self._upload_cache = UploadDedupCache()

        # Background tasks
        self._bg_tasks: set[asyncio.Task] = set()

    # -- Auth headers -------------------------------------------------------

    async def _auth_headers(self) -> Dict[str, str]:
        """Build authorization headers with current access token."""
        if not self._token_manager or not self._http_client:
            raise RuntimeError("Adapter not connected")
        token = await self._token_manager.get_token(self._http_client)
        return {"Authorization": f"QQBot {token}"}

    # -- Connection lifecycle -----------------------------------------------

    async def connect(self) -> bool:
        """Connect to QQ Bot gateway via WebSocket."""
        if not WEBSOCKETS_AVAILABLE:
            logger.warning("[QQ] websockets not installed. Run: pip install websockets")
            return False
        if not HTTPX_AVAILABLE:
            logger.warning("[QQ] httpx not installed. Run: pip install httpx")
            return False
        if not self._app_id or not self._client_secret:
            logger.warning("[QQ] QQ_APP_ID and QQ_CLIENT_SECRET required")
            return False

        try:
            self._http_client = httpx.AsyncClient(timeout=30.0)

            # Initialize token manager
            self._token_manager = TokenManager(self._app_id, self._client_secret)
            await self._token_manager.get_token(self._http_client)
            self._token_manager.start_background_refresh(self._http_client)

            # Get WebSocket gateway URL
            await self._resolve_gateway_url()

            # Start WebSocket connection
            self._ws_task = asyncio.create_task(self._run_ws())
            self._mark_connected()
            logger.info("[QQ] Connected (appId=%s)", self._app_id[:8] + "...")
            return True
        except Exception as e:
            logger.error("[QQ] Failed to connect: %s", e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            return False

    async def _resolve_gateway_url(self) -> None:
        """Fetch WebSocket gateway URL from QQ API."""
        headers = await self._auth_headers()
        resp = await self._http_client.get(QQ_GATEWAY_URL, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        self._ws_url = data["url"]
        logger.debug("[QQ] Gateway URL: %s", self._ws_url)

    async def disconnect(self) -> None:
        """Disconnect from QQ Bot gateway."""
        self._running = False
        self._mark_disconnected()

        # Cancel WebSocket task
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

        # Cancel heartbeat
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        # Close WebSocket
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        # Stop token refresh
        if self._token_manager:
            self._token_manager.clear()
            self._token_manager = None

        # Close HTTP client
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None

        # Cancel background tasks
        for task in self._bg_tasks:
            task.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()

        logger.info("[QQ] Disconnected")

    # -- WebSocket lifecycle ------------------------------------------------

    async def _run_ws(self) -> None:
        """Run WebSocket connection with auto-reconnection."""
        backoff_idx = 0
        while self._running:
            try:
                await self._connect_ws()
                backoff_idx = 0  # Reset on successful connection
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                logger.warning("[QQ] WebSocket error: %s", e)

            if not self._running:
                return

            delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
            logger.info("[QQ] Reconnecting in %ds...", delay)
            await asyncio.sleep(delay)
            backoff_idx += 1

            # Re-resolve gateway URL on reconnect (may change)
            try:
                await self._resolve_gateway_url()
            except Exception as e:
                logger.warning("[QQ] Failed to resolve gateway URL: %s", e)

    async def _connect_ws(self) -> None:
        """Establish WebSocket connection and handle messages."""
        if not self._ws_url:
            raise RuntimeError("No gateway URL resolved")

        async with websockets.connect(
            self._ws_url,
            ping_interval=None,  # We handle heartbeats manually
            close_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info("[QQ] WebSocket connected to %s",
                        self._ws_url[:50] + "...")

            async for raw_message in ws:
                if not self._running:
                    return
                try:
                    data = json.loads(raw_message)
                    await self._on_ws_message(data)
                except json.JSONDecodeError as e:
                    logger.warning("[QQ] Invalid JSON from WS: %s", e)
                except Exception as e:
                    logger.error("[QQ] WS message handling error: %s",
                                 e, exc_info=True)

    async def _on_ws_message(self, data: dict) -> None:
        """Dispatch WebSocket message by opcode."""
        op = data.get("op")
        t = data.get("t")
        d = data.get("d", {})

        if op == 10:  # HELLO
            heartbeat_interval = d.get("heartbeat_interval", 41250) / 1000.0
            await self._send_identify()
            self._start_heartbeat(heartbeat_interval)

        elif op == 11:  # HEARTBEAT ACK
            logger.debug("[QQ] Heartbeat ACK")

        elif op == 0:  # DISPATCH
            await self._on_dispatch(t, d)

        elif op == 7:  # RECONNECT
            logger.info("[QQ] Server requested reconnect")
            if self._ws:
                await self._ws.close()
            self._ws = None

        elif op == 9:  # INVALID SESSION
            logger.warning("[QQ] Invalid session, re-identifying")
            self._session_id = None
            self._seq = None
            await self._send_identify()

        elif op == 2:  # READY (resumed)
            pass

    async def _send_identify(self) -> None:
        """Send IDENTIFY or RESUME payload."""
        if not self._token_manager or not self._http_client:
            return

        token = await self._token_manager.get_token(self._http_client)

        if self._session_id and self._seq is not None:
            # RESUME
            payload = {
                "op": 6,
                "d": {
                    "token": f"QQBot {token}",
                    "session_id": self._session_id,
                    "seq": self._seq,
                },
            }
        else:
            # IDENTIFY
            payload = {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": (1 << 30) | (1 << 12) | (1 << 25) | (1 << 26),  # PUBLIC_GUILD_MESSAGES | DIRECT_MESSAGE | GROUP_AND_C2C | INTERACTION
                    "shard": [0, 1],

                },
            }

        if self._ws:
            await self._ws.send(json.dumps(payload))
            logger.debug("[QQ] Sent %s", "RESUME" if self._session_id else "IDENTIFY")

    def _start_heartbeat(self, interval: float) -> None:
        """Start heartbeat loop."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()

        async def _heartbeat_loop() -> None:
            while self._running:
                try:
                    if self._ws:
                        payload = {"op": 1, "d": self._seq}
                        await self._ws.send(json.dumps(payload))
                        logger.debug("[QQ] Heartbeat sent (seq=%s)", self._seq)
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.warning("[QQ] Heartbeat error: %s", e)
                    await asyncio.sleep(interval)

        self._heartbeat_task = asyncio.create_task(_heartbeat_loop())

    # -- Event dispatch -----------------------------------------------------

    async def _on_dispatch(self, event_type: str, data: dict) -> None:
        """Handle DISPATCH events from QQ gateway."""
        # Track sequence
        if "s" in data:
            self._seq = data["s"]

        if event_type == "READY":
            self._session_id = data.get("session_id")
            user = data.get("user", {})
            self._user_id = user.get("id")
            logger.info("[QQ] READY session=%s user=%s",
                        self._session_id, self._user_id)
            self._mark_connected()
            return

        if event_type == "C2C_MESSAGE_CREATE":
            await self._on_c2c_message(data)
        elif event_type == "GROUP_AT_MESSAGE_CREATE":
            await self._on_group_message(data)
        elif event_type == "INTERACTION_CREATE":
            await self._on_interaction(data)
        else:
            logger.debug("[QQ] Unhandled event type: %s", event_type)

    # -- Inbound: C2C message -----------------------------------------------

    async def _on_c2c_message(self, data: dict) -> None:
        """Process inbound C2C (private) message."""
        msg_id = data.get("id", "")
        if self._dedup.is_duplicate(msg_id):
            logger.debug("[QQ] Duplicate C2C message: %s", msg_id)
            return

        author = data.get("author", {})
        user_openid = author.get("user_openid", "")

        text, media_urls, media_types = await self._extract_content(data)

        # Check slash commands
        if text and text.startswith("/"):
            slash_result = await self._handle_slash_command(text, user_openid, msg_id)
            if slash_result is not None:
                return

        source = self.build_source(
            chat_id=user_openid,
            chat_type="dm",
            user_id=user_openid,
        )

        event = MessageEvent(
            text=text or "",
            message_type=self._detect_message_type(data, media_types),
            source=source,
            message_id=msg_id,
            raw_message=data,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=self._parse_timestamp(data.get("timestamp")),
        )

        logger.debug("[QQ] C2C from %s: %s",
                     user_openid[:8] + "...", (text or "")[:50])
        await self.handle_message(event)

    # -- Inbound: Group message ---------------------------------------------

    async def _on_group_message(self, data: dict) -> None:
        """Process inbound group @mention message."""
        msg_id = data.get("id", "")
        if self._dedup.is_duplicate(msg_id):
            logger.debug("[QQ] Duplicate group message: %s", msg_id)
            return

        group_openid = data.get("group_openid", "")
        author = data.get("author", {})
        user_openid = author.get("member_openid", "")

        # Group policy check
        if not self._should_accept_group_message(group_openid, data):
            logger.debug("[QQ] Group message rejected by policy: %s", group_openid[:8])
            return

        text, media_urls, media_types = await self._extract_content(data)

        # Remove @mention prefix from text
        if text:
            text = re.sub(r'^@\S+\s*', '', text).strip()

        # Check slash commands
        if text and text.startswith("/"):
            slash_result = await self._handle_slash_command(
                text, group_openid, msg_id, is_group=True
            )
            if slash_result is not None:
                return

        source = self.build_source(
            chat_id=group_openid,
            chat_type="group",
            user_id=user_openid,
            user_id_alt=user_openid,
        )

        event = MessageEvent(
            text=text or "",
            message_type=self._detect_message_type(data, media_types),
            source=source,
            message_id=msg_id,
            raw_message=data,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=self._parse_timestamp(data.get("timestamp")),
        )

        logger.debug("[QQ] Group msg from %s/%s: %s",
                     group_openid[:8] + "...", user_openid[:8] + "...",
                     (text or "")[:50])
        await self.handle_message(event)

    # -- Inbound: Interaction -----------------------------------------------

    async def _on_interaction(self, data: dict) -> None:
        """Handle button click interactions."""
        interaction_id = data.get("id", "")
        if not interaction_id:
            return

        # Acknowledge the interaction
        try:
            headers = await self._auth_headers()
            await self._http_client.put(
                f"{QQ_API_BASE}/interactions/{interaction_id}",
                headers=headers,
                json={"code": 0},
                timeout=10.0,
            )
        except Exception as e:
            logger.warning("[QQ] Interaction ack failed: %s", e)

        # Extract button data as text command
        button_data = data.get("data", {})
        resolved = button_data.get("resolved", {})
        button_id = resolved.get("button_id", "")
        if not button_id:
            return

        # Route button click as a command
        chat_id = data.get("group_openid") or ""
        if not chat_id:
            author = data.get("author", {})
            chat_id = author.get("user_openid", "")

        if not chat_id:
            return

        source = self.build_source(
            chat_id=chat_id,
            chat_type="group" if data.get("group_openid") else "dm",
            user_id=(data.get("author", {}).get("member_openid")
                     or data.get("author", {}).get("user_openid", "")),
        )

        event = MessageEvent(
            text=button_id,
            message_type=MessageType.COMMAND,
            source=source,
            message_id=interaction_id,
            raw_message=data,
            timestamp=datetime.now(tz=timezone.utc),
        )
        await self.handle_message(event)

    # -- Content extraction -------------------------------------------------

    async def _extract_content(
        self, data: dict
    ) -> Tuple[str, List[str], List[str]]:
        """Extract text and media attachments from a QQ message.

        Returns (text, media_urls, media_types) where media_urls are
        local file paths to cached media.
        """
        text_parts: List[str] = []
        media_urls: List[str] = []
        media_types: List[str] = []

        # Extract text content
        content = data.get("content", "")
        if content:
            text_parts.append(content.strip())

        # Extract attachments
        attachments = data.get("attachments", [])
        for att in attachments:
            content_type = att.get("content_type", "")
            url = att.get("url", "")
            filename = att.get("filename", "")

            if not url:
                continue

            # Ensure URL has scheme
            if url.startswith("//"):
                url = "https:" + url

            try:
                if content_type.startswith("image/"):
                    ext = Path(filename).suffix if filename else ".jpg"
                    local_path = await cache_image_from_url(url, ext=ext)
                    media_urls.append(local_path)
                    media_types.append("image")

                elif content_type.startswith("audio/") or content_type.startswith("voice/"):
                    local_path = await self._cache_audio_attachment(url, filename)
                    media_urls.append(local_path)
                    media_types.append("audio")

                elif content_type.startswith("video/"):
                    local_path = await self._cache_file_attachment(url, filename or "video.mp4")
                    media_urls.append(local_path)
                    media_types.append("video")

                elif content_type.startswith("application/") or content_type in (
                    "text/plain", "text/csv"
                ):
                    local_path = await self._cache_file_attachment(
                        url, filename or "document"
                    )
                    media_urls.append(local_path)
                    media_types.append("file")
            except Exception as e:
                logger.warning("[QQ] Failed to cache attachment: %s", e)

        text = " ".join(text_parts).strip()
        return text, media_urls, media_types

    async def _cache_audio_attachment(self, url: str, filename: str) -> str:
        """Download and convert audio attachment for STT processing."""
        ext = Path(filename).suffix.lower() if filename else ".ogg"
        raw_data = await self._download_url(url)

        # Convert SILK to WAV for hermes STT tools
        if ext == ".silk" or _is_silk_data(raw_data):
            wav_data = await convert_silk_to_wav(raw_data)
            return cache_audio_from_bytes(wav_data, ext=".wav")

        return cache_audio_from_bytes(raw_data, ext=ext)

    async def _cache_file_attachment(self, url: str, filename: str) -> str:
        """Download a file attachment to cache."""
        raw_data = await self._download_url(url)
        return cache_document_from_bytes(raw_data, filename)

    async def _download_url(self, url: str) -> bytes:
        """Download URL content to bytes with retry."""
        from gateway.platforms.base import _safe_url_for_log
        from tools.url_safety import is_safe_url

        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe attachment URL: {_safe_url_for_log(url)}")

        last_exc: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for attempt in range(3):
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    return resp.content
                except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                    last_exc = exc
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 429:
                        raise
                    if attempt < 2:
                        await asyncio.sleep(1.5 * (attempt + 1))
                        continue
                    raise
        raise last_exc or RuntimeError("Download failed")

    @staticmethod
    def _detect_message_type(data: dict, media_types: List[str]) -> MessageType:
        """Detect message type from QQ event data."""
        if "audio" in media_types or "voice" in media_types:
            return MessageType.VOICE
        if "image" in media_types:
            return MessageType.PHOTO
        if "video" in media_types:
            return MessageType.VIDEO
        if "file" in media_types:
            return MessageType.DOCUMENT
        content = data.get("content", "").strip()
        if content.startswith("/"):
            return MessageType.COMMAND
        return MessageType.TEXT

    @staticmethod
    def _parse_timestamp(ts: Optional[str]) -> datetime:
        """Parse ISO 8601 timestamp from QQ API."""
        if not ts:
            return datetime.now(tz=timezone.utc)
        try:
            # QQ uses ISO 8601 format: 2024-01-01T00:00:00+08:00
            return datetime.fromisoformat(ts)
        except (ValueError, TypeError):
            return datetime.now(tz=timezone.utc)

    # -- Outbound: send -----------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text or markdown message to a C2C or group chat."""
        if not self._http_client:
            return SendResult(success=False, error="Not connected")

        metadata = metadata or {}
        is_group = metadata.get("is_group", False)
        msg_id_for_seq = metadata.get("msg_seq")

        # Determine message type and length limit
        # QQ API requires markdown content in {"markdown": {"content": "..."}}
        # See: https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/type/markdown.html
        if self._markdown_support:
            msg_type = MSG_TYPE_MARKDOWN
            max_len = MAX_MARKDOWN_LENGTH
            content_field = {"markdown": {"content": content[:max_len]}}
        else:
            msg_type = MSG_TYPE_TEXT
            max_len = MAX_TEXT_LENGTH
            content_field = {"content": content[:max_len]}

        # Build payload
        payload: Dict[str, Any] = {
            "msg_type": msg_type,
            **content_field,
        }

        if msg_id_for_seq is not None:
            payload["msg_seq"] = msg_id_for_seq
        elif reply_to:
            payload["msg_seq"] = self._make_msg_seq()

        if reply_to:
            payload["msg_id"] = reply_to

        try:
            headers = await self._auth_headers()
            endpoint = self._build_send_endpoint(chat_id, is_group)
            resp = await self._http_client.post(
                endpoint, json=payload, headers=headers, timeout=15.0
            )

            if resp.status_code < 300:
                resp_data = resp.json()
                return SendResult(
                    success=True,
                    message_id=resp_data.get("id", resp_data.get("msg_id")),
                    raw_response=resp_data,
                )

            error_body = resp.text[:500]
            logger.warning("[QQ] Send failed HTTP %d: %s", resp.status_code, error_body)
            return SendResult(
                success=False,
                error=f"HTTP {resp.status_code}: {error_body}",
            )
        except httpx.TimeoutException:
            return SendResult(success=False, error="Timeout sending to QQ", retryable=True)
        except Exception as e:
            logger.error("[QQ] Send error: %s", e)
            return SendResult(success=False, error=str(e))

    def _build_send_endpoint(self, chat_id: str, is_group: bool) -> str:
        """Build the message send API endpoint."""
        if is_group:
            return f"{QQ_API_BASE}/v2/groups/{chat_id}/messages"
        return f"{QQ_API_BASE}/v2/users/{chat_id}/messages"

    @staticmethod
    def _make_msg_seq() -> int:
        """Generate a message sequence number (16-bit)."""
        import random
        return (int(time.time() * 1000) % 100000000 ^ random.randint(0, 65535)) % 65536

    # -- Outbound: send_image -----------------------------------------------

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image via QQ media upload + message."""
        metadata = metadata or {}
        is_group = metadata.get("is_group", False)

        file_info = await self._upload_media_url(
            image_url, chat_id, is_group, QQFileType.IMAGE
        )
        if not file_info:
            return SendResult(success=False, error="Image upload failed")

        return await self._send_media_message(
            chat_id, file_info, QQFileType.IMAGE,
            caption=caption, reply_to=reply_to, metadata=metadata,
        )

    # -- Outbound: send_image_file ------------------------------------------

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file via QQ media upload."""
        metadata = kwargs.get("metadata") or {}
        is_group = metadata.get("is_group", False)

        file_info = await self._upload_media_file(
            image_path, chat_id, is_group, QQFileType.IMAGE
        )
        if not file_info:
            return SendResult(success=False, error="Image file upload failed")

        return await self._send_media_message(
            chat_id, file_info, QQFileType.IMAGE,
            caption=caption, reply_to=reply_to, metadata=metadata,
        )

    # -- Outbound: send_voice -----------------------------------------------

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a voice message. Converts audio to SILK/WAV/MP3 if needed."""
        metadata = kwargs.get("metadata") or {}
        is_group = metadata.get("is_group", False)

        ext = Path(audio_path).suffix.lower()

        # Check if format is natively supported by QQ
        if ext in self._voice_direct_formats:
            file_info = await self._upload_media_file(
                audio_path, chat_id, is_group, QQFileType.VOICE
            )
        else:
            # Convert to SILK (or WAV fallback)
            silk_data = await convert_audio_to_silk(audio_path)
            if not silk_data:
                return SendResult(success=False, error="Audio conversion failed")

            # Determine output format
            if silk_data[:4] == b"RIFF":
                upload_ext = ".wav"
            else:
                upload_ext = ".silk"

            file_info = await self._upload_media_bytes(
                silk_data, f"voice{upload_ext}", chat_id, is_group,
                QQFileType.VOICE,
            )

        if not file_info:
            return SendResult(success=False, error="Voice upload failed")

        return await self._send_media_message(
            chat_id, file_info, QQFileType.VOICE,
            caption=caption, reply_to=reply_to, metadata=metadata,
        )

    # -- Outbound: send_video -----------------------------------------------

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video file."""
        metadata = kwargs.get("metadata") or {}
        is_group = metadata.get("is_group", False)

        file_info = await self._upload_media_file(
            video_path, chat_id, is_group, QQFileType.VIDEO
        )
        if not file_info:
            return SendResult(success=False, error="Video upload failed")

        return await self._send_media_message(
            chat_id, file_info, QQFileType.VIDEO,
            caption=caption, reply_to=reply_to, metadata=metadata,
        )

    # -- Outbound: send_document --------------------------------------------

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file attachment. Uses chunked upload for large files."""
        metadata = kwargs.get("metadata") or {}
        is_group = metadata.get("is_group", False)

        file_size = os.path.getsize(file_path)
        if file_size > CHUNKED_UPLOAD_THRESHOLD:
            file_info = await self._chunked_upload(
                file_path, chat_id, is_group
            )
        else:
            file_info = await self._upload_media_file(
                file_path, chat_id, is_group, QQFileType.FILE,
            )

        if not file_info:
            return SendResult(success=False, error="Document upload failed")

        return await self._send_media_message(
            chat_id, file_info, QQFileType.FILE,
            caption=caption, reply_to=reply_to, metadata=metadata,
        )

    # -- Media upload -------------------------------------------------------

    async def _upload_media_url(
        self, url: str, chat_id: str, is_group: bool, file_type: int
    ) -> Optional[str]:
        """Upload media from URL to QQ servers. Returns file_info string."""
        scope = "groups" if is_group else "users"
        content_hash = _compute_md5(url.encode())

        # Check dedup cache
        cached = self._upload_cache.get(content_hash, scope, chat_id, file_type)
        if cached:
            return cached

        try:
            headers = await self._auth_headers()
            endpoint = self._build_upload_endpoint(chat_id, is_group)
            payload = {
                "file_type": file_type,
                "url": url,
                "srv_send_msg": False,
            }
            resp = await self._http_client.post(
                endpoint, json=payload, headers=headers, timeout=30.0
            )
            resp.raise_for_status()
            data = resp.json()
            file_info = data.get("file_info") or data.get("FileInfo")
            if file_info:
                self._upload_cache.put(content_hash, scope, chat_id, file_type, file_info)
            return file_info
        except Exception as e:
            logger.warning("[QQ] Media URL upload failed: %s", e)
            return None

    async def _upload_media_file(
        self, file_path: str, chat_id: str, is_group: bool, file_type: int
    ) -> Optional[str]:
        """Upload a local file to QQ servers. Returns file_info string."""
        scope = "groups" if is_group else "users"
        content_hash = _compute_file_md5(file_path)

        # Check dedup cache
        cached = self._upload_cache.get(content_hash, scope, chat_id, file_type)
        if cached:
            return cached

        try:
            data = Path(file_path).read_bytes()
            return await self._upload_media_bytes(
                data, Path(file_path).name, chat_id, is_group, file_type,
                content_hash=content_hash,
            )
        except Exception as e:
            logger.warning("[QQ] Media file upload failed: %s", e)
            return None

    async def _upload_media_bytes(
        self, data: bytes, filename: str, chat_id: str, is_group: bool,
        file_type: int, content_hash: Optional[str] = None,
    ) -> Optional[str]:
        """Upload raw bytes to QQ servers. Returns file_info string."""
        scope = "groups" if is_group else "users"
        if not content_hash:
            content_hash = _compute_md5(data)

        # Check dedup cache
        cached = self._upload_cache.get(content_hash, scope, chat_id, file_type)
        if cached:
            return cached

        import base64
        file_data_b64 = base64.b64encode(data).decode()

        try:
            headers = await self._auth_headers()
            endpoint = self._build_upload_endpoint(chat_id, is_group)
            payload = {
                "file_type": file_type,
                "file_data": file_data_b64,
                "srv_send_msg": False,
                "file_name": filename,
            }
            resp = await self._http_client.post(
                endpoint, json=payload, headers=headers,
                timeout=60.0,
            )
            resp.raise_for_status()
            resp_data = resp.json()
            file_info = resp_data.get("file_info") or resp_data.get("FileInfo")
            if file_info:
                self._upload_cache.put(content_hash, scope, chat_id, file_type, file_info)
            return file_info
        except Exception as e:
            logger.warning("[QQ] Media bytes upload failed: %s", e)
            return None

    def _build_upload_endpoint(self, chat_id: str, is_group: bool) -> str:
        """Build the media upload API endpoint."""
        if is_group:
            return f"{QQ_API_BASE}/v2/groups/{chat_id}/files"
        return f"{QQ_API_BASE}/v2/users/{chat_id}/files"

    # -- Chunked upload -----------------------------------------------------

    async def _chunked_upload(
        self, file_path: str, chat_id: str, is_group: bool
    ) -> Optional[str]:
        """Upload a large file using chunked upload protocol.

        Flow:
          1. upload_prepare  → get upload_id + presigned URLs + block_size
          2. For each part:  PUT to presigned URL → upload_part_finish (per-part)
          3. complete_upload → POST /files with upload_id → get file_info
        """
        file_size = os.path.getsize(file_path)
        filename = Path(file_path).name
        md5_hash, sha1_hash, md5_10m = _compute_streaming_hashes(file_path)

        try:
            headers = await self._auth_headers()

            # Step 1: Prepare upload
            prepare_url = (
                f"{QQ_API_BASE}/v2/groups/{chat_id}/upload_prepare"
                if is_group
                else f"{QQ_API_BASE}/v2/users/{chat_id}/upload_prepare"
            )
            prepare_payload = {
                "file_type": QQFileType.FILE,
                "file_name": filename,
                "file_size": file_size,
                "md5": md5_hash,
                "sha1": sha1_hash,
                "md5_10m": md5_10m,
            }
            resp = await self._http_client.post(
                prepare_url, json=prepare_payload, headers=headers, timeout=30.0
            )
            resp.raise_for_status()
            prepare_data = resp.json()

            upload_id = prepare_data.get("upload_id", "")
            parts = prepare_data.get("upload_info", [])
            block_size = int(prepare_data.get("block_size", MAX_CHUNK_SIZE))

            if not parts:
                # File already exists on server (instant upload via hash match)
                return prepare_data.get("file_info") or prepare_data.get("FileInfo")

            # Step 2: Upload each part → PUT to presigned + upload_part_finish
            part_finish_url = (
                f"{QQ_API_BASE}/v2/groups/{chat_id}/upload_part_finish"
                if is_group
                else f"{QQ_API_BASE}/v2/users/{chat_id}/upload_part_finish"
            )

            for part_info in parts:
                part_index = part_info.get("index", 1)
                presigned_url = part_info.get("presigned_url", "")
                if not presigned_url:
                    continue

                # Read the chunk from file
                offset = (part_index - 1) * block_size
                length = min(block_size, file_size - offset)
                with open(file_path, "rb") as f:
                    f.seek(offset)
                    chunk = f.read(length)

                # Compute per-part MD5
                part_md5 = _compute_md5(chunk)

                # PUT to presigned URL
                await self._put_to_presigned(presigned_url, chunk)

                # Notify server that this part is uploaded
                part_finish_payload = {
                    "upload_id": upload_id,
                    "part_index": part_index,
                    "block_size": length,
                    "md5": part_md5,
                }
                # Retry upload_part_finish on transient errors
                for attempt in range(3):
                    resp = await self._http_client.post(
                        part_finish_url, json=part_finish_payload,
                        headers=headers, timeout=30.0,
                    )
                    if resp.status_code < 300:
                        break
                    try:
                        err_data = resp.json()
                        biz_code = err_data.get("code", 0)
                        if biz_code == 40093002:
                            logger.error("[QQ] Upload daily limit exceeded")
                            return None
                    except Exception:
                        pass
                    if attempt < 2:
                        await asyncio.sleep(2 ** (attempt + 1))
                else:
                    logger.error("[QQ] Part finish failed for part %d", part_index)
                    return None

            # Step 3: Complete upload
            complete_url = (
                f"{QQ_API_BASE}/v2/groups/{chat_id}/files"
                if is_group
                else f"{QQ_API_BASE}/v2/users/{chat_id}/files"
            )
            complete_payload = {"upload_id": upload_id}
            resp = await self._http_client.post(
                complete_url, json=complete_payload, headers=headers, timeout=30.0,
            )
            resp.raise_for_status()
            complete_data = resp.json()
            return complete_data.get("file_info") or complete_data.get("FileInfo")

        except Exception as e:
            logger.error("[QQ] Chunked upload failed: %s", e, exc_info=True)
            return None

    async def _put_to_presigned(self, url: str, data: bytes) -> None:
        """PUT data to a presigned URL with retry."""
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=300.0) as client:
                    resp = await client.put(url, content=data)
                    if resp.status_code < 300:
                        return
                    logger.warning("[QQ] PUT presigned HTTP %d (attempt %d)",
                                   resp.status_code, attempt + 1)
            except Exception as e:
                last_exc = e
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
        raise last_exc or RuntimeError("Presigned PUT failed")

    # -- Media message send -------------------------------------------------

    async def _send_media_message(
        self,
        chat_id: str,
        file_info: str,
        file_type: int,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a media message using file_info from upload."""
        metadata = metadata or {}
        is_group = metadata.get("is_group", False)

        payload: Dict[str, Any] = {
            "msg_type": MSG_TYPE_MEDIA,
            "media": {
                "file_info": file_info,
            },
        }

        if caption:
            payload["content"] = caption[:MAX_TEXT_LENGTH]
        if reply_to:
            payload["msg_id"] = reply_to
            payload["msg_seq"] = self._make_msg_seq()

        try:
            headers = await self._auth_headers()
            endpoint = self._build_send_endpoint(chat_id, is_group)
            resp = await self._http_client.post(
                endpoint, json=payload, headers=headers, timeout=15.0
            )

            if resp.status_code < 300:
                resp_data = resp.json()
                return SendResult(
                    success=True,
                    message_id=resp_data.get("id", resp_data.get("msg_id")),
                    raw_response=resp_data,
                )

            error_body = resp.text[:500]
            logger.warning("[QQ] Media send failed HTTP %d: %s",
                           resp.status_code, error_body)
            return SendResult(
                success=False,
                error=f"HTTP {resp.status_code}: {error_body}",
            )
        except httpx.TimeoutException:
            return SendResult(success=False, error="Timeout sending media", retryable=True)
        except Exception as e:
            logger.error("[QQ] Media send error: %s", e)
            return SendResult(success=False, error=str(e))

    # -- Typing indicator ---------------------------------------------------

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Send typing indicator (C2C only via input_notify)."""
        metadata = metadata or {}
        is_group = metadata.get("is_group", False)

        # QQ only supports input_notify for C2C
        if is_group:
            return

        if not self._http_client:
            return

        try:
            headers = await self._auth_headers()
            endpoint = f"{QQ_API_BASE}/v2/users/{chat_id}/messages"
            payload = {
                "msg_type": MSG_TYPE_INPUT_NOTIFY,
            }
            await self._http_client.post(
                endpoint, json=payload, headers=headers, timeout=10.0
            )
        except Exception:
            pass  # Typing indicator is best-effort

    # -- Group policy -------------------------------------------------------

    def _should_accept_group_message(self, group_openid: str, data: dict) -> bool:
        """Check if a group message should be accepted based on policy."""
        if self._group_policy == "disabled":
            return False

        if self._group_policy == "allowlist":
            return group_openid in self._group_allowlist

        # "open" policy — accept all
        return True

    # -- Slash commands -----------------------------------------------------

    async def _handle_slash_command(
        self, text: str, chat_id: str, msg_id: str,
        is_group: bool = False,
    ) -> Optional[str]:
        """Handle built-in slash commands. Returns response or None if not a command."""
        parts = text.strip().split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if command not in SLASH_COMMANDS:
            return None

        metadata = {"is_group": is_group}

        if command == "/bot-ping":
            start = time.time()
            result = await self.send(
                chat_id=chat_id,
                content="pong",
                reply_to=msg_id,
                metadata=metadata,
            )
            latency = (time.time() - start) * 1000
            if result.success:
                logger.info("[QQ] /bot-ping latency: %.0fms", latency)
            return ""

        elif command == "/bot-version":
            await self.send(
                chat_id=chat_id,
                content=f"QQ Bot Adapter v{ADAPTER_VERSION}\nHermes Agent Gateway",
                reply_to=msg_id,
                metadata=metadata,
            )
            return ""

        elif command == "/bot-help":
            lines = ["**Available Commands:**\n"]
            for cmd, desc in SLASH_COMMANDS.items():
                lines.append(f"  `{cmd}` — {desc}")
            help_text = "\n".join(lines)
            await self.send(
                chat_id=chat_id,
                content=help_text,
                reply_to=msg_id,
                metadata=metadata,
            )
            return ""

        return None

    # -- get_chat_info ------------------------------------------------------

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about a QQ chat."""
        # QQ API doesn't provide a simple chat info endpoint
        return {
            "name": chat_id[:16],
            "type": "group",
            "chat_id": chat_id,
        }

    # -- format_message -----------------------------------------------------

    def format_message(self, content: str) -> str:
        """Format message for QQ platform.

        QQ supports markdown when enabled, otherwise plain text.
        """
        if not self._markdown_support:
            # Strip markdown formatting for plain text mode
            content = re.sub(r'\*\*(.+?)\*\*', r'\1', content)  # bold
            content = re.sub(r'\*(.+?)\*', r'\1', content)      # italic
            content = re.sub(r'`(.+?)`', r'\1', content)        # inline code
            content = re.sub(r'^#{1,6}\s+', '', content, flags=re.MULTILINE)  # headers
        return content
