"""Scan, preview, start camera, stop camera. Reception starts automatically."""
from __future__ import annotations

import asyncio
import errno
import json
import queue
import socket
import threading
import time
import tkinter as tk
from tkinter import ttk

import cv2

from .config import VideoSettings
from .direct import serve_direct
from .lan import (LAN_VERSION, DEFAULT_PORT, LanReceiver, checked_port, data_directory,
                  discover_addresses, pairing_url, qr_image)
from .pairing import load_identity
from .lan_audio import AudioMonitor
from .runtime import VirtualOutput
from . import lan_camera

BG, WHITE, INK, MUTED, BLUE = "#f3f5f9", "#ffffff", "#25354b", "#74849a", "#4264ee"


class LanWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"FaceLive LAN · {LAN_VERSION}")
        self.geometry("1000x700")
        self.minsize(820, 660)
        self.configure(bg=BG)
        self.option_add("*Font", "{Microsoft YaHei UI} 10")
        self.events, self.frame_lock = queue.Queue(), threading.Lock()
        self.audio = AudioMonitor(lambda error: self.emit("audio_error", error))
        self.latest, self.revision, self.displayed, self.picture = None, 0, -1, None
        self.agent = self.identity = self.output = self.service = self.stop_event = None
        self.closing = self.active = self.starting = self.output_requested = self.output_starting = False
        self.connection_link, self.last_video, self.addresses = "", 0, []
        self.settings = VideoSettings(720, 1280, 30, 6000)
        try:
            prefs = json.loads((data_directory() / "settings.json").read_text(encoding="utf-8"))
            self.port = checked_port(prefs.get("port", DEFAULT_PORT))
        except (OSError, ValueError, TypeError, AttributeError):
            self.port = DEFAULT_PORT
        self.status = tk.StringVar(value="正在准备手机连接……")
        self.output_status = tk.StringVar(value="摄像头名称：xxx3")
        self.monitor = tk.BooleanVar(value=False)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="LAN video loop")
        self.thread.start()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(30, self.poll)
        self.background(self._prepare, "prepared", "prepare_failed")
        self.after(10000, self.refresh_network)

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        tasks = asyncio.all_tasks(self.loop)
        for task in tasks:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.run_until_complete(self.loop.shutdown_default_executor())
        self.loop.close()

    def emit(self, kind, value=None):
        self.events.put((kind, value))

    def background(self, function, kind, error_kind="error"):
        def run():
            try:
                self.emit(kind, function())
            except Exception as ex:
                self.emit(error_kind, str(ex))
        threading.Thread(target=run, daemon=True).start()

    def _prepare(self):
        return load_identity(data_directory() / "identity"), discover_addresses()

    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=WHITE)
        style.configure("TLabel", background=WHITE, foreground=INK)
        style.configure("TButton", padding=(10, 11), background=WHITE, foreground=INK, bordercolor="#e1e6ef")
        style.configure("Primary.TButton", background=BLUE, foreground=WHITE)
        style.map("Primary.TButton", background=[("disabled", "#b2bdf4"), ("active", "#3554d6")])
        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, padx=20, pady=20)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)
        self.controls = ttk.Frame(main, padding=18)
        self.controls.grid(row=0, column=0, sticky="ns", padx=(0, 18))
        ttk.Label(self.controls, text="手机扫码连接", font=("Microsoft YaHei UI", 15, "bold")).pack(pady=(6, 8))
        qr_panel = tk.Frame(self.controls, width=270, height=270, bg=WHITE)
        qr_panel.pack(pady=(8, 10))
        qr_panel.pack_propagate(False)
        self.qr = tk.Label(qr_panel, text="正在生成二维码……", bg=WHITE, fg=MUTED)
        self.qr.place(relx=.5, rely=.5, anchor="center")
        ttk.Label(self.controls, textvariable=self.status, wraplength=270, foreground=MUTED,
                  anchor="center", justify="center").pack(fill="x", pady=(0, 18))
        self.output_start = ttk.Button(self.controls, text="启动虚拟摄像头", command=self.start_output, style="Primary.TButton")
        self.output_start.pack(fill="x", pady=(0, 9))
        self.output_stop = ttk.Button(self.controls, text="关闭虚拟摄像头", command=self.stop_output, state="disabled")
        self.output_stop.pack(fill="x")
        ttk.Label(self.controls, textvariable=self.output_status, wraplength=270, foreground=MUTED,
                  anchor="center", justify="center").pack(fill="x", pady=(14, 0))
        ttk.Checkbutton(self.controls, text="监听手机麦克风", variable=self.monitor,
                        command=self.toggle_monitor).pack(anchor="w", pady=(16, 0))
        panel = tk.Frame(main, bg="#141c29")
        panel.grid(row=0, column=1, sticky="nsew")
        self.preview = tk.Label(panel, text="等待手机画面", bg="#141c29", fg="#90a0b6")
        self.preview.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.preview.bind("<Configure>", lambda _: setattr(self, "displayed", -1))

    def save(self):
        directory = data_directory()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "settings.json").write_text(json.dumps({"port": self.port}), encoding="utf-8")

    def refresh_network(self):
        if not self.closing:
            if self.identity:
                self.background(discover_addresses, "addresses")
            self.after(10000, self.refresh_network)

    def refresh_qr(self):
        self.connection_link = ""
        if not self.active or not self.addresses or not self.identity:
            self.qr.configure(image="", text="等待网络连接")
            return
        try:
            link = pairing_url(self.addresses, self.port, self.identity.connection_key, socket.gethostname())
            image = qr_image(link)
            self.qr_picture = tk.PhotoImage(data=cv2.imencode(".png", image)[1].tobytes())
            self.qr.configure(image=self.qr_picture, text="")
            self.connection_link = link
        except Exception as ex:
            self.qr.configure(image="", text="二维码暂不可用")
            self.status.set(str(ex))

    def start(self):
        if self.closing or self.active or self.starting or not self.identity or not self.addresses:
            return
        self.starting = True
        preferred_port = self.port
        async def serve():
            self.stop_event = asyncio.Event()
            self.agent = LanReceiver(self.identity, preview=self.receive_frame,
                on_settings=lambda value: self.emit("settings", value),
                on_disconnect=self.phone_disconnected, on_audio=self.audio.receive)
            try:
                # The QR carries the port, so a busy port needs no settings UI.
                for port in range(preferred_port, min(65536, preferred_port + 10)):
                    try:
                        await serve_direct(self.agent, "0.0.0.0", port, self.stop_event,
                                           on_ready=lambda port=port: self.emit("started", port))
                        return
                    except OSError as ex:
                        if ex.errno != errno.EADDRINUSE and getattr(ex, "winerror", None) != 10048:
                            raise
                raise OSError("端口被占用，请关闭旧版接收端后重新打开。")
            finally:
                self.agent = None
        self.service = asyncio.run_coroutine_threadsafe(serve(), self.loop)
        def finished(future):
            try:
                future.result()
            except Exception as ex:
                self.emit("error", str(ex))
            self.emit("stopped")
        self.service.add_done_callback(finished)

    def receive_frame(self, image):
        with self.frame_lock:
            self.latest, self.last_video = image.copy(), time.monotonic()
            self.revision += 1
            if self.output:
                self.output.submit_image(image)

    def phone_disconnected(self):
        self.audio.reset()
        self.emit("disconnected")

    def toggle_monitor(self):
        if not self.monitor.get():
            self.audio.stop()
            return
        try:
            self.audio.start()
        except OSError as ex:
            self.emit("audio_error", str(ex))

    def _close_output(self):
        with self.frame_lock:
            old, self.output = self.output, None
        if old:
            old.close()

    def _open_output(self):
        self._close_output()
        output = VirtualOutput(self.settings, 0, status=lambda s: self.emit("output_error", s),
                               camera_factory=lan_camera.open_camera)
        output.enabled = True
        with self.frame_lock:
            self.output = output
            if self.latest is not None and time.monotonic() - self.last_video <= 1:
                output.submit_image(self.latest)
        self.output_start.configure(state="disabled")
        self.output_stop.configure(state="normal")
        self.output_status.set("xxx3 已启动")

    def start_output(self):
        if self.output_requested or self.output_starting:
            return
        self.output_requested = self.output_starting = True
        self.output_start.configure(state="disabled")
        self.output_status.set("正在启动 xxx3；首次使用请确认管理员授权。")
        self.background(lan_camera.ensure_installed, "camera_ready", error_kind="camera_failed")

    def stop_output(self):
        self.output_requested = False
        self._close_output()
        self.output_start.configure(state="disabled" if self.output_starting else "normal")
        self.output_stop.configure(state="disabled")
        self.output_status.set("xxx3 已关闭")

    def clear_frame(self):
        with self.frame_lock:
            self.latest = None
            self.revision += 1
            if self.output:
                self.output.clear()
        self.preview.configure(image="", text="等待手机画面")
        self.picture = None

    def poll(self):
        if self.closing:
            return
        for _ in range(80):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "prepared":
                self.identity, self.addresses = value
                self.start()
                if not self.addresses:
                    self.status.set("请让手机和电脑连接同一路由器，连接后会自动显示二维码。")
            elif kind == "prepare_failed":
                self.status.set(value)
                self.qr.configure(text="连接信息准备失败")
            elif kind == "addresses":
                if value != self.addresses:
                    self.addresses = value
                    self.refresh_qr()
                    self.status.set("手机和电脑连接同一路由器后扫码。" if value else "等待局域网连接……")
                self.start()
            elif kind == "started":
                self.active, self.starting, self.port = True, False, value
                self.status.set("AI魔颜 → 局域网 Wi-Fi → 扫码")
                self.refresh_qr()
                try:
                    self.save()
                except OSError:
                    pass
            elif kind == "stopped":
                self.active = self.starting = False
                self.refresh_qr()
                self.clear_frame()
            elif kind == "settings":
                self.settings = value
                self.clear_frame()
                self.status.set("手机已连接")
                if self.output_requested and not self.output_starting:
                    try:
                        self._open_output()
                    except Exception as ex:
                        self.emit("output_error", str(ex))
            elif kind == "disconnected":
                self.clear_frame()
                self.status.set("等待手机重新连接")
            elif kind == "camera_ready":
                self.output_starting = False
                if self.output_requested:
                    try:
                        self._open_output()
                    except Exception as ex:
                        self.emit("output_error", str(ex))
                else:
                    self.output_start.configure(state="normal")
            elif kind in ("camera_failed", "output_error"):
                self.output_starting = False
                self.stop_output()
                self.output_status.set("xxx3 未启动：" + value)
            elif kind == "error":
                self.status.set(value)
            elif kind == "audio_error":
                self.monitor.set(False)
                self.status.set(value)
        with self.frame_lock:
            image = self.latest if self.revision != self.displayed else None
            revision = self.revision
        if image is not None:
            h, w = image.shape[:2]
            scale = min(max(1, self.preview.winfo_width()) / w, max(1, self.preview.winfo_height()) / h)
            resized = cv2.resize(image, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
            self.picture = tk.PhotoImage(data=cv2.imencode(".png", resized)[1].tobytes())
            self.preview.configure(image=self.picture, text="")
            self.displayed = revision
        self.after(16, self.poll)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.audio.stop()
        try:
            self.save()
        except OSError:
            pass
        self._close_output()
        if self.stop_event:
            self.loop.call_soon_threadsafe(self.stop_event.set)
        async def shutdown():
            if self.service:
                try:
                    await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(self.service)), 10)
                except Exception:
                    pass
            self.loop.call_soon(self.loop.stop)
        asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        self.withdraw()
        self.after(50, self._finish_close)

    def _finish_close(self):
        if self.thread.is_alive():
            self.after(50, self._finish_close)
        else:
            self.destroy()
