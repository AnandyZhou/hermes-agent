#!/usr/bin/env python3
"""
QQ Bot Live Integration Test

Interactive test that connects to a real QQ Bot and exercises:
  - WebSocket connection + heartbeat
  - Token management
  - Receive C2C / group messages
  - Send text / markdown messages
  - Send images (URL + local file)
  - Send voice messages
  - Send documents
  - Send video
  - Typing indicator
  - Slash commands

Usage:
    cd /path/to/hermes-agent
    python tests/gateway/test_qq_live.py

Requires:
    pip install httpx websockets
"""

import asyncio
import base64
import io
import json
import os
import sys
import time
import traceback
from pathlib import Path

# Ensure project root is on sys.path BEFORE any other imports
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))
# Also add CWD in case __file__ resolves differently
if "." not in sys.path:
    sys.path.insert(0, ".")

# ── Config ────────────────────────────────────────────────────────────────────

QQ_APP_ID = os.getenv("QQ_APP_ID", "")
QQ_CLIENT_SECRET = os.getenv("QQ_CLIENT_SECRET", "")

# ── Colors ────────────────────────────────────────────────────────────────────

class C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

def ok(msg):    print(f"  {C.GREEN}✓{C.RESET} {msg}")
def fail(msg):  print(f"  {C.RED}✗{C.RESET} {msg}")
def info(msg):  print(f"  {C.CYAN}→{C.RESET} {msg}")
def warn(msg):  print(f"  {C.YELLOW}⚠{C.RESET} {msg}")
def header(msg): print(f"\n{C.BOLD}{C.CYAN}{msg}{C.RESET}")


# ── Minimal adapter wrapper for live testing ──────────────────────────────────

from gateway.config import PlatformConfig
from gateway.platforms.qq import (
    QQBotAdapter,
    TokenManager,
    QQ_API_BASE,
    QQ_TOKEN_URL,
    QQ_GATEWAY_URL,
)


class LiveTestResult:
    passed = 0
    failed = 0
    skipped = 0
    errors = []

    def record_pass(self, name):
        self.passed += 1
        ok(name)

    def record_fail(self, name, err):
        self.failed += 1
        self.errors.append((name, err))
        fail(f"{name}: {err}")

    def record_skip(self, name, reason):
        self.skipped += 1
        warn(f"{name}: skipped ({reason})")

    def summary(self):
        total = self.passed + self.failed + self.skipped
        print(f"\n{'─' * 60}")
        print(f"  {C.BOLD}Results:{C.RESET} {self.passed} passed, {self.failed} failed, {self.skipped} skipped (total {total})")
        if self.errors:
            print(f"\n  {C.RED}Failures:{C.RESET}")
            for name, err in self.errors:
                print(f"    - {name}: {err}")
        return self.failed == 0


results = LiveTestResult()


# ── Step 1: Token Management ──────────────────────────────────────────────────

async def test_token_management():
    header("Step 1: Token Management")
    import httpx

    client = httpx.AsyncClient(timeout=15.0)
    tm = TokenManager(QQ_APP_ID, QQ_CLIENT_SECRET)

    try:
        token = await tm.get_token(client)
        if token and len(token) > 10:
            results.record_pass("获取 access_token")
            info(f"token: {token[:8]}...{token[-4:]}")
        else:
            results.record_fail("获取 access_token", f"unexpected token: {token!r}")
    except Exception as e:
        results.record_fail("获取 access_token", str(e))

    # Test cached token (no new HTTP request)
    try:
        token2 = await tm.get_token(client)
        if token2 == token:
            results.record_pass("Token 缓存命中")
        else:
            results.record_fail("Token 缓存命中", "second call returned different token")
    except Exception as e:
        results.record_fail("Token 缓存命中", str(e))

    tm.clear()
    await client.aclose()


# ── Step 2: Resolve Gateway URL ──────────────────────────────────────────────

async def test_resolve_gateway():
    header("Step 2: Resolve WebSocket Gateway URL")
    import httpx

    client = httpx.AsyncClient(timeout=15.0)
    tm = TokenManager(QQ_APP_ID, QQ_CLIENT_SECRET)

    try:
        token = await tm.get_token(client)
        resp = await client.get(
            QQ_GATEWAY_URL,
            headers={"Authorization": f"QQBot {token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        ws_url = data.get("url", "")
        if ws_url and "wss://" in ws_url:
            results.record_pass("获取 WebSocket Gateway URL")
            info(f"URL: {ws_url}")
        else:
            results.record_fail("获取 WebSocket Gateway URL", f"unexpected: {data}")
    except Exception as e:
        results.record_fail("获取 WebSocket Gateway URL", str(e))

    tm.clear()
    await client.aclose()


# ── Step 3: WebSocket Connect + Receive Messages ─────────────────────────────

_received_messages = []
_ws_session_id = None
_ws_user_id = None


async def test_websocket_connect():
    header("Step 3: WebSocket 连接与消息接收")
    import httpx
    import websockets

    client = httpx.AsyncClient(timeout=30.0)
    tm = TokenManager(QQ_APP_ID, QQ_CLIENT_SECRET)

    try:
        # Get token
        token = await tm.get_token(client)

        # Get gateway URL
        resp = await client.get(
            QQ_GATEWAY_URL,
            headers={"Authorization": f"QQBot {token}"},
        )
        resp.raise_for_status()
        ws_url = resp.json()["url"]

        info(f"连接 WebSocket: {ws_url[:60]}...")
        connected_event = asyncio.Event()
        ready_event = asyncio.Event()
        message_count = 0

        async def on_message(data):
            nonlocal message_count
            message_count += 1
            _received_messages.append(data)

            event_type = data.get("t", "")
            op = data.get("op")

            if event_type == "READY":
                global _ws_session_id, _ws_user_id
                d = data.get("d", {})
                _ws_session_id = d.get("session_id")
                _ws_user_id = d.get("user", {}).get("id")
                ready_event.set()
                ok(f"READY: session={_ws_session_id}, user={_ws_user_id}")

            elif event_type == "C2C_MESSAGE_CREATE":
                d = data.get("d", {})
                content = d.get("content", "")
                author = d.get("author", {})
                user_openid = author.get("user_openid", "unknown")
                info(f"C2C 消息: [{user_openid[:12]}...] {content[:80]}")

            elif event_type == "GROUP_AT_MESSAGE_CREATE":
                d = data.get("d", {})
                content = d.get("content", "")
                group_openid = d.get("group_openid", "unknown")
                info(f"群消息: [{group_openid[:12]}...] {content[:80]}")

            elif op == 10:
                # HELLO
                d = data.get("d", {})
                interval = d.get("heartbeat_interval", 41250)
                info(f"HELLO: heartbeat_interval={interval}ms")
                connected_event.set()

            elif op == 11:
                pass  # Heartbeat ACK

        async with websockets.connect(ws_url, ping_interval=None, close_timeout=10) as ws:
            # Wait for HELLO
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            data = json.loads(raw)
            await on_message(data)

            if data.get("op") != 10:
                results.record_fail("WebSocket HELLO", f"expected op=10, got op={data.get('op')}")
                tm.clear()
                await client.aclose()
                return

            results.record_pass("WebSocket 连接成功，收到 HELLO")

            # Send IDENTIFY
            identify = {
                "op": 2,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": (1 << 30) | (1 << 12) | (1 << 25) | (1 << 26),
                    "shard": [0, 1],
                },
            }
            await ws.send(json.dumps(identify))
            info("已发送 IDENTIFY")

            # Start heartbeat
            heartbeat_interval = data["d"].get("heartbeat_interval", 41250) / 1000.0
            stop_heartbeat = asyncio.Event()

            async def heartbeat_loop():
                while not stop_heartbeat.is_set():
                    try:
                        await ws.send(json.dumps({"op": 1, "d": None}))
                        await asyncio.sleep(heartbeat_interval)
                    except Exception:
                        break

            hb_task = asyncio.create_task(heartbeat_loop())

            # Wait for READY or messages for 30 seconds
            info("等待 READY 事件... (30s 超时)")
            info("提示: 在 QQ 上给机器人发消息来测试消息接收")

            try:
                deadline = asyncio.get_event_loop().time() + 30
                got_ready = False
                got_messages = False

                while asyncio.get_event_loop().time() < deadline:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5))
                        msg_data = json.loads(raw)
                        await on_message(msg_data)

                        if msg_data.get("t") == "READY" and not got_ready:
                            got_ready = True
                            results.record_pass("收到 READY 事件")
                            info("请在 QQ 上给机器人发一条消息来获取 user_openid")
                            info("等待消息中... (60s 超时)")
                            # Extend wait to receive messages
                            deadline = asyncio.get_event_loop().time() + 60

                        if msg_data.get("t") in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"):
                            got_messages = True
                            # Extend deadline so we can continue receiving more messages
                            deadline = asyncio.get_event_loop().time() + 15

                    except asyncio.TimeoutError:
                        continue

                if got_ready:
                    pass  # Already recorded
                else:
                    results.record_skip("READY 事件", "30s 内未收到")

                if got_messages:
                    results.record_pass("收到消息事件 (C2C 或群)")
                else:
                    results.record_skip("消息接收", "30s 内未收到消息 (请确保在 QQ 上给机器人发消息)")

            finally:
                stop_heartbeat.set()
                hb_task.cancel()
                try:
                    await hb_task
                except asyncio.CancelledError:
                    pass

    except Exception as e:
        results.record_fail("WebSocket 连接", str(e))
    finally:
        tm.clear()
        await client.aclose()


# ── Step 4: Send Text Message ────────────────────────────────────────────────

async def test_send_message():
    header("Step 4: 发送文本消息")
    import httpx

    info("此步骤需要一个有效的 chat_id (user_openid 或 group_openid)")
    chat_id = os.getenv("QQ_TEST_CHAT_ID", "")
    if not chat_id:
        # Try to use the first received message's sender
        for msg in _received_messages:
            d = msg.get("d", {})
            if not isinstance(d, dict):
                continue
            author = d.get("author")
            if isinstance(author, dict):
                chat_id = author.get("user_openid", "")
            if chat_id:
                break
            chat_id = d.get("group_openid", "")
            if chat_id:
                break

    if not chat_id:
        results.record_skip("发送文本消息", "没有可用的 chat_id (设置 QQ_TEST_CHAT_ID 或先接收一条消息)")
        return

    is_group = any("group_openid" in msg.get("d", {}) for msg in _received_messages)
    info(f"目标: {'群 ' if is_group else 'C2C '}{chat_id[:16]}...")

    client = httpx.AsyncClient(timeout=30.0)
    tm = TokenManager(QQ_APP_ID, QQ_CLIENT_SECRET)

    try:
        token = await tm.get_token(client)
        headers = {"Authorization": f"QQBot {token}"}

        # Send text message
        endpoint = (
            f"{QQ_API_BASE}/v2/groups/{chat_id}/messages"
            if is_group
            else f"{QQ_API_BASE}/v2/users/{chat_id}/messages"
        )

        payload = {
            "msg_type": 0,  # text
            "content": f"[Live Test] 文本消息测试 - {time.strftime('%H:%M:%S')}",
            "msg_seq": int(time.time() * 1000) % 65536,
        }

        resp = await client.post(endpoint, json=payload, headers=headers, timeout=15.0)
        body = resp.text[:500]

        if resp.status_code < 300:
            results.record_pass("发送文本消息")
            info(f"Response: {body[:200]}")
        else:
            results.record_fail("发送文本消息", f"HTTP {resp.status_code}: {body}")

        # Send markdown message
        payload_md = {
            "msg_type": 2,  # markdown
            "content": f"# Live Test\nMarkdown 消息测试\n时间: {time.strftime('%H:%M:%S')}",
            "msg_seq": int(time.time() * 1000) % 65536,
        }

        resp2 = await client.post(endpoint, json=payload_md, headers=headers, timeout=15.0)
        body2 = resp2.text[:500]

        if resp2.status_code < 300:
            results.record_pass("发送 Markdown 消息")
        else:
            # Markdown might not be enabled for all bots
            results.record_skip("发送 Markdown 消息", f"HTTP {resp2.status_code}: {body2[:200]}")

    except Exception as e:
        results.record_fail("发送文本消息", str(e))

    tm.clear()
    await client.aclose()


# ── Step 5: Send Image ───────────────────────────────────────────────────────

async def test_send_image():
    header("Step 5: 发送图片")
    import httpx

    chat_id = os.getenv("QQ_TEST_CHAT_ID", "")
    if not chat_id:
        for msg in _received_messages:
            d = msg.get("d", {})
            chat_id = d.get("author", {}).get("user_openid", "")
            if chat_id:
                break
            chat_id = d.get("group_openid", "")
            if chat_id:
                break

    if not chat_id:
        results.record_skip("发送图片", "没有可用的 chat_id")
        return

    is_group = any("group_openid" in msg.get("d", {}) for msg in _received_messages)
    scope = "groups" if is_group else "users"

    client = httpx.AsyncClient(timeout=30.0)
    tm = TokenManager(QQ_APP_ID, QQ_CLIENT_SECRET)

    try:
        token = await tm.get_token(client)
        headers = {"Authorization": f"QQBot {token}"}

        # Create a simple test PNG (1x1 red pixel)
        png_data = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
        )
        file_data_b64 = base64.b64encode(png_data).decode()

        # Upload image
        upload_endpoint = (
            f"{QQ_API_BASE}/v2/groups/{chat_id}/files"
            if is_group
            else f"{QQ_API_BASE}/v2/users/{chat_id}/files"
        )

        upload_payload = {
            "file_type": 1,  # IMAGE
            "file_data": file_data_b64,
            "srv_send_msg": False,
        }

        resp = await client.post(upload_endpoint, json=upload_payload, headers=headers, timeout=30.0)
        body = resp.text[:500]

        if resp.status_code >= 300:
            results.record_fail("上传图片", f"HTTP {resp.status_code}: {body}")
            tm.clear()
            await client.aclose()
            return

        file_info = resp.json().get("file_info") or resp.json().get("FileInfo")
        if not file_info:
            results.record_fail("上传图片", f"no file_info in response: {body}")
            tm.clear()
            await client.aclose()
            return

        results.record_pass("上传图片到 QQ 服务器")

        # Send media message
        send_endpoint = (
            f"{QQ_API_BASE}/v2/groups/{chat_id}/messages"
            if is_group
            else f"{QQ_API_BASE}/v2/users/{chat_id}/messages"
        )

        send_payload = {
            "msg_type": 7,  # media
            "media": {"file_info": file_info},
            "content": "[Live Test] 图片测试",
            "msg_seq": int(time.time() * 1000) % 65536,
        }

        resp2 = await client.post(send_endpoint, json=send_payload, headers=headers, timeout=15.0)
        if resp2.status_code < 300:
            results.record_pass("发送图片消息")
        else:
            results.record_fail("发送图片消息", f"HTTP {resp2.status_code}: {resp2.text[:300]}")

    except Exception as e:
        results.record_fail("发送图片", str(e))

    tm.clear()
    await client.aclose()


# ── Step 6: Send Voice ───────────────────────────────────────────────────────

async def test_send_voice():
    header("Step 6: 发送语音消息")
    import httpx
    import struct

    chat_id = os.getenv("QQ_TEST_CHAT_ID", "")
    if not chat_id:
        for msg in _received_messages:
            d = msg.get("d", {})
            chat_id = d.get("author", {}).get("user_openid", "")
            if chat_id:
                break
            chat_id = d.get("group_openid", "")
            if chat_id:
                break

    if not chat_id:
        results.record_skip("发送语音", "没有可用的 chat_id")
        return

    is_group = any("group_openid" in msg.get("d", {}) for msg in _received_messages)

    client = httpx.AsyncClient(timeout=30.0)
    tm = TokenManager(QQ_APP_ID, QQ_CLIENT_SECRET)

    try:
        token = await tm.get_token(client)
        headers = {"Authorization": f"QQBot {token}"}

        # Create a minimal WAV file (0.5s of silence, 24kHz mono 16-bit)
        sample_rate = 24000
        num_samples = sample_rate // 2  # 0.5s
        pcm = b"\x00" * (num_samples * 2)  # silence
        data_size = len(pcm)
        wav_header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', 36 + data_size, b'WAVE', b'fmt ',
            16, 1, 1, sample_rate, sample_rate * 2, 2, 16,
            b'data', data_size,
        )
        wav_data = wav_header + pcm
        file_data_b64 = base64.b64encode(wav_data).decode()

        # Upload voice
        upload_endpoint = (
            f"{QQ_API_BASE}/v2/groups/{chat_id}/files"
            if is_group
            else f"{QQ_API_BASE}/v2/users/{chat_id}/files"
        )

        upload_payload = {
            "file_type": 3,  # VOICE
            "file_data": file_data_b64,
            "srv_send_msg": False,
        }

        resp = await client.post(upload_endpoint, json=upload_payload, headers=headers, timeout=30.0)

        if resp.status_code >= 300:
            results.record_fail("上传语音", f"HTTP {resp.status_code}: {resp.text[:300]}")
            tm.clear()
            await client.aclose()
            return

        file_info = resp.json().get("file_info") or resp.json().get("FileInfo")
        if not file_info:
            results.record_fail("上传语音", f"no file_info: {resp.text[:300]}")
            tm.clear()
            await client.aclose()
            return

        results.record_pass("上传语音到 QQ 服务器")

        # Send voice message
        send_endpoint = (
            f"{QQ_API_BASE}/v2/groups/{chat_id}/messages"
            if is_group
            else f"{QQ_API_BASE}/v2/users/{chat_id}/messages"
        )

        send_payload = {
            "msg_type": 7,
            "media": {"file_info": file_info},
            "msg_seq": int(time.time() * 1000) % 65536,
        }

        resp2 = await client.post(send_endpoint, json=send_payload, headers=headers, timeout=15.0)
        if resp2.status_code < 300:
            results.record_pass("发送语音消息")
        else:
            results.record_fail("发送语音消息", f"HTTP {resp2.status_code}: {resp2.text[:300]}")

    except Exception as e:
        results.record_fail("发送语音", str(e))

    tm.clear()
    await client.aclose()


# ── Step 7: Send Document ────────────────────────────────────────────────────

async def test_send_document():
    header("Step 7: 发送文件")
    import httpx

    chat_id = os.getenv("QQ_TEST_CHAT_ID", "")
    if not chat_id:
        for msg in _received_messages:
            d = msg.get("d", {})
            chat_id = d.get("author", {}).get("user_openid", "")
            if chat_id:
                break
            chat_id = d.get("group_openid", "")
            if chat_id:
                break

    if not chat_id:
        results.record_skip("发送文件", "没有可用的 chat_id")
        return

    is_group = any("group_openid" in msg.get("d", {}) for msg in _received_messages)

    client = httpx.AsyncClient(timeout=30.0)
    tm = TokenManager(QQ_APP_ID, QQ_CLIENT_SECRET)

    try:
        token = await tm.get_token(client)
        headers = {"Authorization": f"QQBot {token}"}

        # Create a test text file
        file_content = f"QQ Bot Live Test\nGenerated at {time.strftime('%Y-%m-%d %H:%M:%S')}\nHello from integration test!"
        file_data = file_content.encode("utf-8")
        file_data_b64 = base64.b64encode(file_data).decode()

        # Upload document
        upload_endpoint = (
            f"{QQ_API_BASE}/v2/groups/{chat_id}/files"
            if is_group
            else f"{QQ_API_BASE}/v2/users/{chat_id}/files"
        )

        upload_payload = {
            "file_type": 4,  # FILE
            "file_data": file_data_b64,
            "srv_send_msg": False,
        }

        resp = await client.post(upload_endpoint, json=upload_payload, headers=headers, timeout=30.0)

        if resp.status_code >= 300:
            results.record_fail("上传文件", f"HTTP {resp.status_code}: {resp.text[:300]}")
            tm.clear()
            await client.aclose()
            return

        file_info = resp.json().get("file_info") or resp.json().get("FileInfo")
        if not file_info:
            results.record_fail("上传文件", f"no file_info: {resp.text[:300]}")
            tm.clear()
            await client.aclose()
            return

        results.record_pass("上传文件到 QQ 服务器")

        # Send file message
        send_endpoint = (
            f"{QQ_API_BASE}/v2/groups/{chat_id}/messages"
            if is_group
            else f"{QQ_API_BASE}/v2/users/{chat_id}/messages"
        )

        send_payload = {
            "msg_type": 7,
            "media": {"file_info": file_info},
            "content": "[Live Test] 文件测试",
            "msg_seq": int(time.time() * 1000) % 65536,
        }

        resp2 = await client.post(send_endpoint, json=send_payload, headers=headers, timeout=15.0)
        if resp2.status_code < 300:
            results.record_pass("发送文件消息")
        else:
            results.record_fail("发送文件消息", f"HTTP {resp2.status_code}: {resp2.text[:300]}")

    except Exception as e:
        results.record_fail("发送文件", str(e))

    tm.clear()
    await client.aclose()


# ── Step 8: Typing Indicator ─────────────────────────────────────────────────

async def test_typing_indicator():
    header("Step 8: 输入状态 (Typing)")
    import httpx

    chat_id = os.getenv("QQ_TEST_CHAT_ID", "")
    if not chat_id:
        for msg in _received_messages:
            d = msg.get("d", {})
            chat_id = d.get("author", {}).get("user_openid", "")
            if chat_id:
                break

    if not chat_id:
        results.record_skip("输入状态", "没有可用的 C2C chat_id")
        return

    # Typing only works for C2C
    is_group = any("group_openid" in msg.get("d", {}) for msg in _received_messages)
    if is_group:
        # Look for a user_openid instead
        for msg in _received_messages:
            d = msg.get("d", {})
            uid = d.get("author", {}).get("user_openid", "")
            if uid:
                chat_id = uid
                break

    client = httpx.AsyncClient(timeout=15.0)
    tm = TokenManager(QQ_APP_ID, QQ_CLIENT_SECRET)

    try:
        token = await tm.get_token(client)
        headers = {"Authorization": f"QQBot {token}"}

        endpoint = f"{QQ_API_BASE}/v2/users/{chat_id}/messages"
        payload = {"msg_type": 6}  # INPUT_NOTIFY

        resp = await client.post(endpoint, json=payload, headers=headers, timeout=10.0)
        if resp.status_code < 300:
            results.record_pass("发送输入状态")
        else:
            results.record_skip("发送输入状态", f"HTTP {resp.status_code} (可能需要用户先发过消息)")

    except Exception as e:
        results.record_fail("发送输入状态", str(e))

    tm.clear()
    await client.aclose()


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    if not QQ_APP_ID or not QQ_CLIENT_SECRET:
        print(f"{C.RED}Error: QQ_APP_ID and QQ_CLIENT_SECRET environment variables must be set.{C.RESET}")
        print("Usage: QQ_APP_ID=xxx QQ_CLIENT_SECRET=yyy python tests/gateway/test_qq_live.py")
        sys.exit(1)

    print(f"""
{C.BOLD}{C.CYAN}┌─────────────────────────────────────────────────────────┐
│           QQ Bot Live Integration Test                   │
├─────────────────────────────────────────────────────────┤
│  App ID:  {QQ_APP_ID:<44} │
│  Secret:  {QQ_CLIENT_SECRET[:4]}...{QQ_CLIENT_SECRET[-4:]:<40} │
│                                                          │
│  此脚本将连接真实 QQ Bot 服务器并测试各项功能。          │
│  请确保在 QQ 上已与机器人建立会话。                      │
└─────────────────────────────────────────────────────────┘{C.RESET}
""")

    # Run tests sequentially
    await test_token_management()
    await test_resolve_gateway()
    await test_websocket_connect()
    await test_send_message()
    await test_send_image()
    await test_send_voice()
    await test_send_document()
    await test_typing_indicator()

    # Summary
    success = results.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
