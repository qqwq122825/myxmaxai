"""Phone-compatible LAN reception. TLS and bearer authentication stay enabled."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
import cv2
import numpy as np
import qrcode

from .config import VideoSettings, video_limits
from .direct import DirectAgent, ReceiveClock, SendChannel, parse_control
from .direct_codec import H264Decoder, MAX_MESSAGE
from .media import FrameSink
from .lan_audio import AUDIO_MAGIC, MAX_PCM_BYTES
from .pairing import ConnectionKey, load_identity

LAN_VERSION = "1.0.5"
DEFAULT_PORT = 8767  # Does not take the existing remote bridge's default port.
NETWORKS = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"))


def data_directory():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "FaceLiveLan"


def is_lan_ip(value):
    try:
        ip = ipaddress.IPv4Address(value)
        return any(ip in network for network in NETWORKS)
    except ipaddress.AddressValueError:
        return False


def checked_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValueError("端口须为 1024–65535 的整数。") from None
    if not 1024 <= port <= 65535:
        raise ValueError("端口须为 1024–65535 的整数。")
    return port


def parse_addresses(value):
    addresses = list(dict.fromkeys(value.replace("，", ",").replace(";", ",").replace(" ", ",").split(",")))
    addresses = [item.strip() for item in addresses if item.strip()]
    if not addresses or any(not is_lan_ip(item) for item in addresses):
        raise ValueError("请填写本机局域网 IPv4 地址，多个地址用英文逗号分隔，例如 192.168.1.23。")
    if len(addresses) > 8:
        raise ValueError("最多填写 8 个局域网地址。")
    return addresses


def discover_addresses():
    candidates = []
    # UDP connect asks the OS for its default route; it sends no application data.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))
            candidates.append(probe.getsockname()[0])
        except OSError:
            pass
    if os.name == "nt":
        try:
            result = subprocess.run([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "@(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | "
                "Where-Object {$_.AddressState -eq 'Preferred'} | "
                "Select-Object -ExpandProperty IPAddress) | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode == 0 and result.stdout.strip():
                addresses = json.loads(result.stdout.lstrip("\ufeff"))
                candidates.extend([addresses] if isinstance(addresses, str) else addresses)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    try:
        candidates.extend(item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    return list(dict.fromkeys(ip for ip in candidates if is_lan_ip(ip)))[:8]


def pairing_url(addresses, port, key, name=""):
    addresses = parse_addresses(",".join(addresses))
    port = checked_port(port)
    ConnectionKey.decode(key)
    return "aimoyan://connect?" + urlencode({"v": 1, "mode": "lan",
        "addr": ",".join(f"{ip}:{port}" for ip in addresses), "key": key, "name": name[:40]})


def qr_image(value, size=270):
    # Use the QR library for encoding and an independent decoder for validation.
    # Some long credential patterns are poorly detected with a particular mask;
    # try another standard mask instead of displaying an unverified pairing code.
    for mask in (None, *range(8)):
        code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                             border=4, mask_pattern=mask)
        code.add_data(value)
        code.make(fit=True)
        matrix = np.where(np.array(code.get_matrix()), 0, 255).astype(np.uint8)
        scale = max(1, size // matrix.shape[0])
        result = cv2.resize(matrix, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        if cv2.QRCodeDetector().detectAndDecode(result)[0] == value:
            return result
    raise ValueError("二维码生成校验失败，请先复制 Key 手动连接，或减少二维码中的网卡地址。")


def rotate_key(identity):
    """Call while stopped. Keep TLS identity, invalidate previous phone credentials."""
    key = secrets.token_urlsafe(32)
    path = identity.certificate.parent / "access.key"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(key, encoding="ascii")
    temporary.chmod(0o600)
    temporary.replace(path)
    return load_identity(path.parent)


class LanReceiver(DirectAgent):
    def __init__(self, identity, *, preview=None, status=lambda _: None,
                 statistics=lambda _: None, on_settings=lambda _: None, on_disconnect=lambda: None,
                 on_audio=lambda _: None):
        super().__init__(identity, preview=preview, status=status)
        self.statistics, self.on_settings, self.on_disconnect = statistics, on_settings, on_disconnect
        self.on_audio = on_audio
        # A live phone sends ~30 frames and a ping every second. A connection silent for longer than this
        # is a phone that dropped off Wi-Fi without closing; the next phone with the Key may replace it.
        self.stale_after = 3.0
        self.last_activity = 0.0

    def connection_stale(self):
        return self.busy and time.monotonic() - self.last_activity > self.stale_after

    def live_busy(self):
        return self.busy and not self.connection_stale()

    async def health(self, request):
        return web.json_response({"version": LAN_VERSION, "protocol": "direct-1",
            "features": ["lan-receiver", "lan-mic-pcm"], "busy": self.live_busy(), "video_limits": video_limits()})

    async def release_stale_connection(self):
        """Close a silent previous connection so a reconnecting phone is not refused for 10–20 s."""
        old = self.ws if self.connection_stale() else None
        if old is None:
            return
        self.status("旧连接已无响应，正在切换到新的手机连接…")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(old.close(code=1001, message=b"Replaced by a new connection"), 2)
        deadline = time.monotonic() + 5
        while self.busy and self.ws is old and time.monotonic() < deadline:
            await asyncio.sleep(.05)

    async def stream(self, request):
        ws = web.WebSocketResponse(heartbeat=5, receive_timeout=20,
                                  max_msg_size=MAX_MESSAGE, compress=False)
        await ws.prepare(request)
        claimed, sink, reporter = False, None, None
        try:
            first = await asyncio.wait_for(ws.receive(), 10)
            if first.type != aiohttp.WSMsgType.TEXT:
                raise ValueError("缺少视频会话参数。")
            hello = parse_control(first.data)
            if hello.get("type") != "start" or hello.get("protocol") != "direct-1" or hello.get("role") != "source":
                raise ValueError("此接收端只接受手机局域网推流（direct-1 / source）。")
            settings = VideoSettings(**hello["video"])
            await self.release_stale_connection()
            async with self.lock:
                if self.busy:
                    raise ValueError("已经连着另一台手机，请先断开旧连接。")
                self.busy, claimed, self.ws = True, True, ws
                self.last_activity = time.monotonic()
            self.on_settings(settings)
            sink = FrameSink(settings, "none", self.preview)
            await sink.wait_ready()
            decoder, clock, channel = H264Decoder(settings), ReceiveClock(), SendChannel(ws)
            await channel.send({"type": "ready", "protocol": "direct-1", "mode": "lan",
                                "video": asdict(settings)})
            self.status(f"手机已连接 · {settings.width}×{settings.height} / {settings.fps} FPS · 等待画面")
            received_frames = received_bytes = 0
            last_frame = time.monotonic()

            async def report():
                previous_frames = previous_bytes = 0
                previous_time = time.monotonic()
                while True:
                    await asyncio.sleep(1)
                    now = time.monotonic()
                    if now - last_frame > 15:
                        raise TimeoutError("15 秒未收到手机画面，请检查手机推流和 Wi‑Fi。")
                    if sink.error:
                        raise sink.error
                    dt = now - previous_time
                    self.statistics({"fps": (received_frames - previous_frames) / dt,
                        "mbps": (received_bytes - previous_bytes) * 8 / dt / 1e6,
                        "frames": received_frames, "dropped": sink.dropped,
                        "width": settings.width, "height": settings.height,
                        "target_kbps": settings.bitrate_kbps})
                    previous_frames, previous_bytes, previous_time = received_frames, received_bytes, now

            async def receive():
                nonlocal received_frames, received_bytes, last_frame
                async for message in ws:
                    self.last_activity = time.monotonic()
                    if message.type == aiohttp.WSMsgType.BINARY:
                        if message.data.startswith(AUDIO_MAGIC):
                            pcm = message.data[len(AUDIO_MAGIC):]
                            if 0 < len(pcm) <= MAX_PCM_BYTES and len(pcm) % 2 == 0:
                                self.on_audio(pcm)
                            continue
                        received_bytes += len(message.data)
                        for frame in await asyncio.to_thread(decoder.decode, message.data):
                            clock.check(frame)
                            received_frames += 1
                            last_frame = time.monotonic()
                            sink.submit(frame)
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        data = parse_control(message.data)
                        if data.get("type") == "ping" and type(data.get("time")) in (float, int):
                            await channel.send({"type": "pong", "time": data["time"]})
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError("局域网视频连接异常。")

            from .direct import wait_for_streams
            reporter = asyncio.create_task(report())
            await wait_for_streams([asyncio.create_task(receive()), reporter])
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(ws.send_json({"type": "error", "message": str(ex)}), 1)
            if claimed:
                self.status(f"手机连接已结束：{ex}")
        finally:
            if reporter:
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
            if sink:
                await sink.close()
            if claimed and self.ws is ws:
                self.busy, self.ws = False, None
                self.on_disconnect()
                self.statistics({"fps": 0, "mbps": 0, "disconnected": True})
                self.status("等待手机重新扫码／连接；接收服务仍在运行。")
            with contextlib.suppress(Exception):
                await ws.close()
        return ws


def bundle_directory():
    return Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


def run_elevated_powershell(script, arguments):
    """Used only by explicit install/firewall buttons. Wait off the UI thread."""
    return run_elevated("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *arguments])


def run_elevated(program, arguments):
    """Run a known helper with UAC and wait, without shell interpolation."""
    import ctypes as ct
    from ctypes import wintypes as wt
    class ShellInfo(ct.Structure):
        _fields_ = [("cbSize", wt.DWORD), ("fMask", ct.c_ulong), ("hwnd", wt.HWND),
            ("lpVerb", wt.LPCWSTR), ("lpFile", wt.LPCWSTR), ("lpParameters", wt.LPCWSTR),
            ("lpDirectory", wt.LPCWSTR), ("nShow", ct.c_int), ("hInstApp", wt.HINSTANCE),
            ("lpIDList", ct.c_void_p), ("lpClass", wt.LPCWSTR), ("hkeyClass", wt.HKEY),
            ("dwHotKey", wt.DWORD), ("hIcon", wt.HANDLE), ("hProcess", wt.HANDLE)]
    info = ShellInfo()
    info.cbSize, info.fMask = ct.sizeof(info), 0x40
    info.lpVerb, info.lpFile, info.nShow = "runas", str(program), 0
    info.lpParameters = subprocess.list2cmdline(arguments)
    shell = ct.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteExW.argtypes, shell.ShellExecuteExW.restype = [ct.POINTER(ShellInfo)], wt.BOOL
    if not shell.ShellExecuteExW(ct.byref(info)):
        if ct.get_last_error() == 1223:
            raise RuntimeError("已取消管理员授权，可稍后再次点击。")
        raise ct.WinError(ct.get_last_error())
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wt.HANDLE, ct.POINTER(wt.DWORD)]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    try:
        if kernel.WaitForSingleObject(info.hProcess, 120000) != 0:
            raise RuntimeError("操作等待超时，请检查安装报告。")
        code = wt.DWORD()
        if not kernel.GetExitCodeProcess(info.hProcess, ct.byref(code)) or code.value:
            raise RuntimeError("操作失败，请检查 FaceLiveLan 下的安装报告／防火墙报告。")
    finally:
        kernel.CloseHandle(info.hProcess)
