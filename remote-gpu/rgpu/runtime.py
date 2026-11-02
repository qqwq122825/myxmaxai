"""Role-specific preferences, public address discovery and independently switched video output."""
from __future__ import annotations

import contextlib
import ipaddress
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import numpy as np

from .media import fit_bgr
from .pairing import ConnectionKey, Pairing


def preferences_path(role):
    if role not in ("local", "agent"):
        raise ValueError("未知运行端。")
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "FaceLiveRemoteGpu"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{role}-settings.json"


def load_preferences(role):
    try:
        value = json.loads(preferences_path(role).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_preferences(role, value):
    path = preferences_path(role)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def public_ip():
    for address in ("https://api.ipify.org", "https://checkip.amazonaws.com"):
        try:
            with urlopen(Request(address, headers={"User-Agent": "FaceLiveRemoteGpu"}), timeout=4) as response:
                value = response.read(128).decode("ascii").strip()
            if ipaddress.ip_address(value).is_global:
                return value
        except Exception:
            pass
    raise RuntimeError("公网 IP 自动检测失败。请填写租机平台显示的公网 IP 或域名。")


def pairing_from_fields(address, key, fingerprint=""):
    key = key.strip()
    if key.startswith("RGK1."):
        credential = ConnectionKey.decode(key)
        key, fingerprint = credential.key, credential.fingerprint
    address = address.strip().rstrip("/")
    if not address.startswith("https://"):
        address = "https://" + address
    try:
        parts = urlsplit(address)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or parts.path):
            raise ValueError()
        return Pairing(parts.hostname, parts.port or 8766, fingerprint.strip().lower().replace(":", ""), key.strip())
    except ValueError as ex:
        if not fingerprint.strip():
            raise ValueError("请复制新版服务器的完整 Key（RGK1. 开头），无需另外填写配对码。") from None
        raise ValueError("服务器地址或 Key 无效，请检查地址和端口，并重新复制服务器 Key。") from ex


class VirtualOutput:
    """A virtual camera has its own lifetime, separate from any network connection."""
    def __init__(self, settings, channel, status=lambda _: None):
        self.settings, self.channel, self.status = settings, channel, status
        self.enabled = False
        self.written = 0
        self._latest = None
        self._diagnostic = None
        self._diagnostic_until = 0
        self._last = 0
        self._gate = threading.Lock()
        self.exit = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"Independent virtual output {channel}", daemon=True)
        self.thread.start()

    def submit_image(self, image):
        with self._gate:
            self._latest = image.copy()
            self._last = time.monotonic()

    def clear(self):
        with self._gate:
            self._latest, self._last = None, 0

    def diagnostic_image(self, image=None):
        """A temporary test image used only while no fresh real input is available."""
        with self._gate:
            self._diagnostic = None if image is None else image.copy()
            self._diagnostic_until = time.monotonic() + 15 if image is not None else 0

    def has_fresh_input(self):
        with self._gate:
            return self._latest is not None and time.monotonic() - self._last <= 1

    def _run(self):
        camera = None
        black = np.zeros((self.settings.height, self.settings.width, 3), np.uint8)
        try:
            while not self.exit.is_set():
                tick = time.monotonic()
                if camera and not self.enabled:
                    camera.send(black)
                    camera.close()
                    camera = None
                if self.enabled and camera is None:
                    try:
                        from .unity import UnityCamera
                        camera = UnityCamera(self.settings.width, self.settings.height, self.channel)
                    except Exception as ex:
                        self.enabled = False
                        self.status(f"虚拟摄像头未开启：{ex}")
                if camera:
                    with self._gate:
                        image = self._latest if tick - self._last <= 1 else None
                        if image is None and tick < self._diagnostic_until:
                            image = self._diagnostic
                    try:
                        image = black if image is None else fit_bgr(image, self.settings.width, self.settings.height)
                        self.written += bool(camera.send(image))
                    except Exception as ex:
                        self.enabled = False
                        self.status(f"虚拟摄像头输出失败：{ex}")
                self.exit.wait(max(0.001, 1 / self.settings.fps - (time.monotonic() - tick)))
        finally:
            if camera:
                with contextlib.suppress(Exception):
                    camera.send(black)
                camera.close()

    def close(self):
        self.exit.set()
        self.thread.join(3)
