from __future__ import annotations

import ipaddress
import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiortc import RTCConfiguration, RTCIceServer

MAX_VIDEO_EDGE = 1920
MAX_VIDEO_PIXELS = 1920 * 1080
VIDEO_PRESETS = {
    "720P 横屏 · 1280×720": (1280, 720),
    "720P 竖屏 · 720×1280": (720, 1280),
    "1080P 横屏 · 1920×1080": (1920, 1080),
    "1080P 竖屏 · 1080×1920": (1080, 1920),
    "360P 流畅 · 640×360": (640, 360),
}


def video_limits():
    return {"max_width": MAX_VIDEO_EDGE, "max_height": MAX_VIDEO_EDGE,
            "max_pixels": MAX_VIDEO_PIXELS, "max_fps": 30, "max_bitrate_kbps": 15000}


def check_remote_video_size(health, settings):
    # Releases before 0.4.6 have no advertised limits and accept at most 720p.
    limits = health.get("video_limits", {})
    defaults = {"max_width": 1280, "max_height": 1280, "max_pixels": 1280 * 720}
    requested = {"max_width": settings.width, "max_height": settings.height,
                 "max_pixels": settings.width * settings.height}
    for name, value in requested.items():
        limit = limits.get(name, defaults[name]) if isinstance(limits, dict) else defaults[name]
        if type(limit) is not int or limit < value:
            raise ValueError("服务器尚不支持所选视频尺寸；1080p 请先将服务器更新至 0.4.6 或更新版本，或改选 720p。")


def private_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address.lower() == "localhost"
    networks = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "::1/128", "fc00::/7")
    return any(ip.version == net.version and ip in net for net in map(ipaddress.ip_network, networks))


def signaling_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("服务器地址应为 http://100.x.x.x:8765 或 HTTPS 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("服务器地址不能包含账号、密码、查询参数或额外路径。")
    if parsed.scheme == "http" and not private_address(parsed.hostname):
        raise ValueError("明文 HTTP 仅允许局域网 / Tailscale IP；公网连接请使用 HTTPS。")
    return value


def check_token(value: str) -> str:
    value = value.strip()
    if len(value) < 24 or len(value) > 256 or not value.isascii() or any(c.isspace() for c in value):
        raise ValueError("连接密钥须为 24–256 位无空格 ASCII 字符；建议使用自动生成的密钥。")
    return value


def token_file(path: Path) -> str:
    if path.exists():
        return check_token(path.read_text(encoding="utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(32)
    with path.open("x", encoding="utf-8") as file:
        file.write(value)
    path.chmod(0o600)
    return value


@dataclass(frozen=True)
class VideoSettings:
    width: int = 1280
    height: int = 720
    fps: int = 30
    bitrate_kbps: int = 4000

    def __post_init__(self):
        if any(type(v) is not int for v in asdict(self).values()):
            raise ValueError("视频参数必须为整数。")
        if not (160 <= self.width <= MAX_VIDEO_EDGE and 120 <= self.height <= MAX_VIDEO_EDGE
                and self.width * self.height <= MAX_VIDEO_PIXELS
                and self.width % 4 == 0 and self.height % 2 == 0):
            raise ValueError("最多支持 1080p（1920×1080 或 1080×1920），宽须为 4 的倍数、高须为偶数。")
        if not 5 <= self.fps <= 30 or not 500 <= self.bitrate_kbps <= 15000:
            raise ValueError("帧率须为 5–30，目标码率须为 500–15000 Kbps。")


def ice_configuration(path: str | None = None) -> RTCConfiguration:
    # No external STUN requests by default: LAN / Tailscale first.
    if not path:
        return RTCConfiguration(iceServers=[])
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return RTCConfiguration(iceServers=[RTCIceServer(**item) for item in data["iceServers"]])


def configure_h264(kbps: int):
    # aiortc has no public setParameters bitrate API. These constants are tested
    # against the exact version in requirements.txt; congestion feedback still
    # adjusts the sender target down to MIN_BITRATE.
    import aiortc
    import aiortc.codecs.h264 as h264
    if aiortc.__version__ != "1.15.0":
        raise RuntimeError("当前原型固定使用 aiortc 1.15.0，请按 requirements.txt 安装。")
    h264.DEFAULT_BITRATE = kbps * 1000
    h264.MIN_BITRATE = 300_000
    h264.MAX_BITRATE = kbps * 1000


def prefer_h264(pc):
    from aiortc import RTCRtpSender
    codecs = [c for c in RTCRtpSender.getCapabilities("video").codecs if c.mimeType.lower() == "video/h264"]
    if not codecs:
        raise RuntimeError("当前运行环境缺少 H.264 编解码器。")
    for transceiver in pc.getTransceivers():
        if transceiver.kind == "video":
            transceiver.setCodecPreferences(codecs)
