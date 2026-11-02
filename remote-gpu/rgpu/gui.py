"""Separate local/server desktop interfaces. No role switching or shared form state."""
from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from concurrent.futures import CancelledError
import queue
import math
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import cv2

from . import __version__
from .bridge import ManagedAgent, ResultSource
from .config import VideoSettings
from .direct import connect_direct, connect_viewer, serve_direct
from .doctor import report, save_report
from .media import fit_bgr, list_cameras, select_camera
from .pairing import ConnectionKey, Pairing, load_identity
from .paths import reports_directory
from .preview import preview_local
from .runtime import (VirtualOutput, load_preferences, pairing_from_fields,
                      public_ip, save_preferences)
from .window_capture import list_windows

BG, WHITE, INK, MUTED, BLUE, LINE = "#f3f5f9", "#ffffff", "#25354b", "#7d8ba1", "#4264ee", "#e4e9f1"
PREVIEW_POLL_HZ = 60  # Check for newest video at display cadence, not the old 100 ms UI poll.


LOCAL_MODES = ["本机摄像头推送并接收结果", "只接收（手机推流）"]


class EventLoop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="Video session loop")
        self.thread.start()
        self.ready.wait(3)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.run_until_complete(self.loop.shutdown_asyncgens())
        self.loop.close()

    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)


class Application(tk.Tk):
    def __init__(self, role="local", preview_camera=None, portrait=False):
        super().__init__()
        if role not in ("local", "agent"):
            raise ValueError("未知运行端。")
        self.role, self.server = role, role == "agent"
        self.title(f"FaceLive Remote · {'服务器端' if self.server else '本地端'} · {__version__}")
        self.geometry("1380x900")
        self.minsize(1080, 760)
        self.configure(bg=BG)
        self.option_add("*Font", "{Microsoft YaHei UI} 10")
        self.messages = queue.Queue()
        self.frames_lock = threading.Lock()
        self.frames = {"input": None, "output": None}
        self.revisions = {"input": 0, "output": 0}
        self.displayed = {"input": -1, "output": -1}
        self.presented_revisions = {"input": 0, "output": 0}
        self.preview_counts = {"input": 0, "output": 0}
        self.preview_times = {name: deque(maxlen=360) for name in ("input", "output")}
        self.preview_rates = {name: tk.StringVar(value="预览显示 — FPS") for name in ("input", "output")}
        self.preview_sample_counts = dict(self.preview_counts)
        self.preview_sample_time = time.perf_counter()
        self.images, self.labels, self.metric_vars = {}, {}, {}
        self.engine = EventLoop()
        self.identity = self.agent = self.virtual_output = None
        self.network_future = self.preview_future = None
        self.preview_task = None
        self.network_stop = self.preview_stop = None
        self.pending_tasks = set()
        self.devices, self.windows = [], []
        self.capture_ready, self.returning, self.closing = False, False, False
        self.settings = VideoSettings(720, 1280) if portrait else VideoSettings()
        prefs = load_preferences(role)
        self.address = tk.StringVar(value=prefs.get("address", ""))
        self.key = tk.StringVar()
        self.fingerprint = tk.StringVar(value=prefs.get("fingerprint", ""))
        self.public_host = tk.StringVar(value=prefs.get("public_host", ""))
        self.listen_port = tk.StringVar(value=str(prefs.get("listen_port", 8766)))
        self.public_port = tk.StringVar(value=str(prefs.get("public_port", 8766)))
        self.generated_code = tk.StringVar()
        self.resolution = tk.StringVar(value="720P 竖屏 · 720×1280" if portrait else prefs.get("resolution", "720P 横屏 · 1280×720"))
        self.fps = tk.StringVar(value=str(prefs.get("fps", 30)))
        self.bitrate = tk.StringVar(value=str(prefs.get("bitrate", 4000)))
        self.work_mode = tk.StringVar(value=prefs.get("work_mode", LOCAL_MODES[0]))
        self.source_type = tk.StringVar(value="独立窗口")
        self.method = tk.StringVar(value="Windows 图形采集（推荐）")
        self.crop_vars = [tk.StringVar(value="0") for _ in range(4)]
        self.use_input_vcam = tk.BooleanVar(value=bool(prefs.get("input_vcam", True)))
        self.status = tk.StringVar(value="准备服务器 Key……" if self.server else "填写服务器地址和 Key，再点击“启动推送”。")
        self.capture_status = tk.StringVar(value="尚未采集结果")
        self.output_status = tk.StringVar(value="虚拟摄像头未开启")
        self.ip_status = tk.StringVar(value="自动识别公网出口 IP；可改为平台提供的入口地址。")
        self.input_text = tk.StringVar(value="等待本地端接入" if self.server else "先预览本地输入画面")
        self.result_text = tk.StringVar(value="选择结果窗口，预览后开始回传" if self.server else "等待服务器回传结果")
        self.default_camera_name = prefs.get("camera", "")
        self.pending_preview_camera = preview_camera
        self._build()
        if not self.server:
            self.work_mode.trace_add("write", lambda *_: self.update_mode_labels())
            self.update_mode_labels()
        self.protocol("WM_DELETE_WINDOW", self.close_window)
        self.after(70, self.poll)
        self.after(150, self.refresh_devices)
        if self.server:
            self.after(200, self.prepare_server)
            self.after(350, self.refresh_windows)
            if not self.public_host.get():
                self.after(400, lambda: self.detect_ip(automatic=True))
            self.public_host.trace_add("write", lambda *_: self.update_pairing())
            self.public_port.trace_add("write", lambda *_: self.update_pairing())

    def emit(self, kind, value=None):
        self.messages.put((kind, value))

    def background(self, function, kind):
        def run():
            try:
                self.emit(kind, function())
            except Exception as ex:
                self.emit("error", str(ex))
        threading.Thread(target=run, daemon=True).start()

    def schedule(self, coroutine, success=None):
        future = self.engine.submit(coroutine)
        self.pending_tasks.add(future)
        def finished(item):
            self.emit("task_done", item)
            try:
                value = item.result()
                if success:
                    self.emit(success, value)
            except (CancelledError, asyncio.CancelledError):
                pass
            except Exception as ex:
                self.emit("error", str(ex))
        future.add_done_callback(finished)
        return future

    def _build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=WHITE)
        style.configure("TLabel", background=WHITE, foreground=INK)
        style.configure("Muted.TLabel", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("TButton", padding=(10, 7), background=WHITE, bordercolor=LINE, foreground="#5c6d88")
        style.map("TButton", background=[("active", "#f0f3fc")])
        style.configure("Primary.TButton", background=BLUE, foreground=WHITE, bordercolor=BLUE)
        style.map("Primary.TButton", background=[("disabled", "#aab9f4"), ("active", "#3456dd")], foreground=[("disabled", WHITE)])
        style.configure("TEntry", padding=6, fieldbackground="#fafbfd", bordercolor=LINE)
        style.configure("TCombobox", padding=5, fieldbackground="#fafbfd", bordercolor=LINE)
        style.configure("TCheckbutton", background=WHITE, foreground=INK)
        header = tk.Frame(self, bg=WHITE, height=76)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="▣", font=("Segoe UI", 25), bg=BLUE, fg=WHITE, width=2).pack(side="left", padx=(24, 13), pady=14)
        titles = tk.Frame(header, bg=WHITE)
        titles.pack(side="left")
        tk.Label(titles, text="FaceLive Remote", font=("Segoe UI", 18, "bold"), bg=WHITE, fg=INK).pack(anchor="w")
        tk.Label(titles, text="服务器端 · 远程 GPU 电脑" if self.server else "本地端 · 我的电脑", bg=WHITE, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        tk.Label(header, text=f"独立{'服务器' if self.server else '本地'}端  ·  {__version__}", fg=BLUE, bg="#edf1ff", padx=14, pady=6).pack(side="right", padx=26)
        main = tk.Frame(self, bg=BG)
        main.pack(fill="both", expand=True, padx=22, pady=20)
        main.rowconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        side = tk.Frame(main, bg=WHITE, width=344, highlightbackground=LINE, highlightthickness=1)
        side.grid(row=0, column=0, sticky="nsew", padx=(0, 22))
        side.grid_propagate(False)
        side.columnconfigure(0, weight=1)
        side.rowconfigure(1, weight=1)
        title = ttk.Frame(side, padding=(20, 18, 20, 14))
        title.grid(row=0, sticky="ew")
        ttk.Label(title, text="接入与回传" if self.server else "连接与采集", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(title, text="接收原画，采集处理结果并回传" if self.server else "发送本地画面，接收处理结果", style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        scroll = ttk.Frame(side)
        scroll.grid(row=1, column=0, sticky="nsew")
        scroll.rowconfigure(0, weight=1)
        scroll.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(scroll, bg=WHITE, highlightthickness=0, width=320)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(scroll, orient="vertical", command=self.canvas.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=bar.set)
        self.form = ttk.Frame(self.canvas, padding=(20, 0, 14, 15))
        window = self.canvas.create_window((0, 0), window=self.form, anchor="nw")
        self.form.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-int(e.delta / 120), "units"))
        self._server_form() if self.server else self._local_form()
        actions = ttk.Frame(side, padding=(20, 12, 20, 16))
        actions.grid(row=2, column=0, sticky="ew")
        self.start_button = ttk.Button(actions, text="启动接收" if self.server else "启动推送", command=self.start, style="Primary.TButton")
        self.start_button.pack(fill="x")
        self.stop_button = ttk.Button(actions, text="停止服务" if self.server else "停止推送", command=self.stop_work, state="disabled")
        self.stop_button.pack(fill="x", pady=(8, 0))
        ttk.Label(actions, text="接收与结果回传分别控制" if self.server else "推送与虚拟摄像头分别控制", style="Muted.TLabel").pack(pady=(9, 0))
        content = tk.Frame(main, bg=BG)
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(2, weight=1)
        titlebar = tk.Frame(content, bg=BG)
        titlebar.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        tk.Label(titlebar, text="服务器画面工作台" if self.server else "本地画面工作台", bg=BG, fg=INK, font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        self.single = tk.BooleanVar(value=False)
        ttk.Checkbutton(titlebar, text="仅结果画面", variable=self.single, command=self.change_view).pack(side="right")
        ttk.Label(content, textvariable=self.status, wraplength=900, padding=12, foreground="#6177a6", background="#edf1ff").grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.preview_panel = tk.Frame(content, bg="#253146")
        self.preview_panel.grid(row=2, column=0, sticky="nsew")
        self.preview_panel.rowconfigure(1, weight=1)
        self.preview_panel.columnconfigure((0, 1), weight=1, uniform="preview")
        self.preview_boxes = {}
        for column, (name, caption, variable) in enumerate((
            ("input", "收到的原始画面 → Camera #2" if self.server else "本地发送画面", self.input_text),
            ("output", "结果来源预览 / 回传画面" if self.server else "服务器回传画面", self.result_text))):
            box = tk.Frame(self.preview_panel, bg="#141c29")
            box.grid(row=0, column=column, rowspan=2, sticky="nsew", padx=(0, 1) if column == 0 else 0)
            box.rowconfigure(1, weight=1)
            box.columnconfigure(0, weight=1)
            tk.Label(box, text=caption, bg="#141c29", fg="#aab9d0", anchor="w", padx=15, pady=14, font=("Microsoft YaHei UI", 9)).grid(row=0, column=0, sticky="ew")
            label = tk.Label(box, bg="#141c29", fg="#778ba8", textvariable=variable, font=("Microsoft YaHei UI", 10), anchor="center", width=1, height=1)
            label.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 12))
            tk.Label(box, textvariable=self.preview_rates[name], bg="#141c29", fg="#7e92af",
                     font=("Microsoft YaHei UI", 8), anchor="e", padx=14, pady=6).grid(row=2, column=0, sticky="ew")
            self.labels[name], self.preview_boxes[name] = label, box
        tk.Label(content, text="接收原画 → 输入虚拟摄像头 → 换脸软件 → 采集结果 → 回传" if self.server else "本地采集 → 远程处理 → 返回结果 → 本地虚拟摄像头 → 直播伴侣", bg=WHITE, fg=MUTED, pady=12, font=("Microsoft YaHei UI", 9)).grid(row=3, column=0, sticky="ew")
        metrics = ttk.Frame(content, padding=(8, 14))
        metrics.grid(row=4, column=0, sticky="ew", pady=14)
        for i, (key, label, suffix) in enumerate((("input_fps", "接收帧率" if self.server else "发送帧率", "FPS"), ("return_fps", "回传帧率", "FPS"), ("rtt_ms", "网络往返 RTT", "ms"), ("bandwidth", "上传 / 下载", "Mbps"))):
            metrics.columnconfigure(i, weight=1, uniform="metrics")
            box = ttk.Frame(metrics, padding=(12, 0))
            box.grid(row=0, column=i, sticky="ew")
            ttk.Label(box, text=label, style="Muted.TLabel").pack(anchor="w")
            v = tk.StringVar(value="—")
            self.metric_vars[key] = v
            ttk.Label(box, textvariable=v, font=("Segoe UI", 17, "bold"), foreground="#526784").pack(anchor="w", pady=(5, 0))
            ttk.Label(box, text=suffix + (" · 非完整画面延迟" if key == "rtt_ms" else ""), style="Muted.TLabel").pack(anchor="w")
        logbar = ttk.Frame(content, padding=(8, 4))
        logbar.grid(row=5, column=0, sticky="ew")
        self.log_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(logbar, text="运行日志", variable=self.log_open, command=self.toggle_logs).pack(side="left")
        ttk.Button(logbar, text="环境检测", command=self.diagnose).pack(side="right")
        self.logs = tk.Text(content, height=5, bg=WHITE, fg="#7285a1", font=("Consolas", 9), relief="flat", state="disabled")
        self.logs.grid(row=6, column=0, sticky="ew", pady=(4, 0))
        self.logs.grid_remove()
        tk.Label(content, text="公网直连 · TLS 加密 · 无需账号  |  H.264 软件编码  |  视频传输，不包含音频", bg=BG, fg="#94a1b6", font=("Microsoft YaHei UI", 8), anchor="w").grid(row=7, column=0, sticky="ew", pady=(10, 0))

    def section(self, number, title):
        frame = ttk.Frame(self.form)
        frame.pack(fill="x", pady=(13, 0))
        ttk.Separator(frame).pack(fill="x", pady=(0, 11))
        ttk.Label(frame, text=f"0{number}  {title}", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(0, 8))
        return frame

    def help(self, parent, text=None, variable=None):
        ttk.Label(parent, text=text, textvariable=variable, style="Muted.TLabel", wraplength=280, justify="left").pack(fill="x", pady=(6, 2))

    def field(self, parent, title, variable=None, values=None, secret=False, readonly=False):
        ttk.Label(parent, text=title, style="Muted.TLabel").pack(anchor="w", pady=(7, 4))
        if values is not None:
            widget = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly")
        else:
            widget = ttk.Entry(parent, textvariable=variable, show="●" if secret else "", state="readonly" if readonly else "normal")
        widget.pack(fill="x")
        return widget

    def _local_form(self):
        f = self.section(1, "服务器连接")
        self.field(f, "服务器地址", self.address)
        self.key_entry = self.field(f, "连接 Key", self.key, secret=True)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(8, 0))
        ttk.Button(row, text="粘贴 Key", command=self.paste_key).pack(side="left")
        ttk.Button(row, text="显示 / 隐藏 Key", command=self.show_key).pack(side="right")
        self.help(f, "只填服务器地址和 Key 即可。地址可写 IP:端口，不写端口时默认 8766。")
        f = self.section(2, "本地画面")
        self.field(f, "工作模式", self.work_mode, LOCAL_MODES)
        self.help(f, "选“只接收（手机推流）”时，手机 App 推送摄像头画面，这台电脑不采集、不上传，只接收服务器转发的结果。"
                     "画面尺寸用于虚拟摄像头输出。")
        self.camera_box = self.field(f, "采集来源", values=["测试画面"])
        self.camera_box.current(0)
        self.field(f, "画面尺寸", self.resolution, ["720P 横屏 · 1280×720", "720P 竖屏 · 720×1280", "360P 流畅 · 640×360"])
        row = ttk.Frame(f)
        row.pack(fill="x")
        for variable, name in ((self.fps, "帧率 FPS"), (self.bitrate, "码率 Kbps")):
            col = ttk.Frame(row)
            col.pack(side="left", fill="x", expand=True, padx=(0, 5))
            self.field(col, name, variable)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(10, 0))
        self.preview_button = ttk.Button(row, text="仅本地预览", command=self.start_preview)
        self.preview_button.pack(side="left")
        ttk.Button(row, text="刷新设备", command=self.refresh_devices).pack(side="right")
        f = self.section(3, "接收与直播输出")
        self.vcam_button = ttk.Button(f, text="开启虚拟摄像头", command=self.toggle_virtual)
        self.vcam_button.pack(fill="x")
        self.help(f, variable=self.output_status)
        self.help(f, "在直播伴侣中选择 FaceLive Camera。没有回传时输出黑色等待画面。")

    def _server_form(self):
        f = self.section(1, "服务器接入")
        self.config_frame = ttk.Frame(f)
        self.config_frame.pack(fill="x")
        c = self.config_frame
        self.field(c, "对外连接 IP / 域名", self.public_host)
        ttk.Button(c, text="重新识别公网 IP", command=self.detect_ip).pack(fill="x", pady=(6, 0))
        self.help(c, variable=self.ip_status)
        for title, variable in (("本机监听端口", self.listen_port), ("平台公网端口", self.public_port)):
            self.field(c, title, variable)
        self.key_entry = self.field(c, "连接 Key（自动生成）", self.key, secret=True, readonly=True)
        row = ttk.Frame(c)
        row.pack(fill="x", pady=(7, 0))
        ttk.Button(row, text="复制 Key", command=lambda: self.copy_text(self.key.get())).pack(side="left")
        ttk.Button(row, text="显示 / 隐藏", command=self.show_key).pack(side="right")
        self.help(c, "本地端只填写服务器地址和这里的 Key，不需要配对码。Key 与本机服务器对应，修改 IP / 端口无需换 Key。")
        self.config_toggle = ttk.Button(f, text="收起连接设置", command=self.toggle_config)
        self.config_toggle.pack(fill="x", pady=(7, 0))
        f = self.section(2, "接收并提供换脸输入")
        ttk.Checkbutton(f, text="输入虚拟摄像头 · Camera #2", variable=self.use_input_vcam, command=self.toggle_input_virtual).pack(anchor="w")
        self.help(f, "换脸软件的输入选择 FaceLive Camera #2；收到原画后持续输出。")
        f = self.section(3, "采集结果并回传")
        self.result_group = f
        self.type_box = self.field(f, "结果来源", self.source_type, ["独立窗口", "结果虚拟摄像头", "原画回环（测试网络）", "测试画面（仅调试）"])
        self.type_box.bind("<<ComboboxSelected>>", lambda _: self.change_source_type())
        self.window_fields = ttk.Frame(f)
        self.window_fields.pack(fill="x")
        self.window_box = self.field(self.window_fields, "采集窗口", values=["点击刷新窗口"])
        self.window_box.current(0)
        ttk.Button(self.window_fields, text="刷新窗口", command=self.refresh_windows).pack(fill="x", pady=(7, 0))
        self.field(self.window_fields, "采集方式", self.method, ["Windows 图形采集（推荐）", "兼容采集 · PrintWindow"])
        self.help(self.window_fields, "保持结果窗口非最小化；窗口关闭或采集失败时停止回传，不改为采集桌面。")
        self.crop_open = tk.BooleanVar(value=False)
        self.crop_frame = ttk.Frame(self.window_fields)
        ttk.Checkbutton(self.window_fields, text="裁切窗口边缘（像素）", variable=self.crop_open, command=lambda: self.crop_frame.pack(fill="x") if self.crop_open.get() else self.crop_frame.pack_forget()).pack(anchor="w", pady=(7, 0))
        for title, variable in zip(("左", "上", "右", "下"), self.crop_vars):
            col = ttk.Frame(self.crop_frame)
            col.pack(side="left", fill="x", expand=True, padx=(0, 4))
            ttk.Label(col, text=title, style="Muted.TLabel").pack()
            ttk.Entry(col, textvariable=variable, width=4).pack(fill="x")
        self.camera_fields = ttk.Frame(f)
        self.result_camera = self.field(self.camera_fields, "结果摄像头", values=["请刷新设备"])
        self.result_camera.current(0)
        ttk.Button(self.camera_fields, text="刷新设备", command=self.refresh_devices).pack(fill="x", pady=(7, 0))
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(11, 0))
        ttk.Button(row, text="预览结果", command=self.preview_result).pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.return_button = ttk.Button(row, text="开始回传", command=self.toggle_return, style="Primary.TButton")
        self.return_button.pack(side="left", fill="x", expand=True)
        ttk.Button(f, text="停止结果采集", command=self.stop_result).pack(fill="x", pady=(7, 0))
        self.help(f, variable=self.capture_status)

    def toggle_config(self):
        if self.config_frame.winfo_manager():
            self.config_frame.pack_forget()
            self.config_toggle.configure(text="展开连接设置 · " + (self.public_host.get() or "尚无 IP"))
        else:
            self.config_frame.pack(fill="x", before=self.config_toggle)
            self.config_toggle.configure(text="收起连接设置")

    def change_source_type(self):
        self.window_fields.pack_forget()
        self.camera_fields.pack_forget()
        if self.source_type.get() == "独立窗口":
            self.window_fields.pack(fill="x", after=self.type_box)
        elif self.source_type.get() == "结果虚拟摄像头":
            self.camera_fields.pack(fill="x", after=self.type_box)
        if self.agent and self.capture_ready:
            self.stop_result()

    def show_key(self):
        self.key_entry.configure(show="" if self.key_entry.cget("show") else "●")

    def copy_text(self, text):
        if not text:
            self.emit("error", "连接配置还未准备好。")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.emit("status", "连接信息已复制。")

    def copy_pairing(self):
        self.update_pairing()
        self.copy_text(self.generated_code.get())

    def update_pairing(self):
        if self.identity:
            try:
                self.generated_code.set(self.identity.pairing(self.public_host.get(), int(self.public_port.get())).encode())
            except (ValueError, TypeError):
                self.generated_code.set("")

    def import_pairing(self):
        try:
            pair = Pairing.decode(self.clipboard_get())
            self.address.set(pair.endpoint)
            self.key.set(pair.key)
            self.fingerprint.set(pair.fingerprint)
            self.save_settings()
            self.emit("status", "已导入服务器地址、Key 和证书校验信息。")
        except (ValueError, tk.TclError) as ex:
            messagebox.showerror("无法导入", "请先在服务器复制完整的 RGPU1 配对码。" if isinstance(ex, tk.TclError) else str(ex), parent=self)

    def paste_key(self):
        try:
            value = self.clipboard_get().strip()
            if value.startswith("RGPU1."):
                # Quiet compatibility for existing copied configurations.
                pair = Pairing.decode(value)
                value = ConnectionKey(pair.key, pair.fingerprint).encode()
                self.address.set(pair.endpoint)
            ConnectionKey.decode(value)
            self.key.set(value)
            self.fingerprint.set("")
            self.emit("status", "Key 已填入；确认服务器地址后点击“启动推送”。")
        except (ValueError, tk.TclError) as ex:
            messagebox.showerror("无法粘贴 Key", "剪贴板没有可用的服务器 Key。" if isinstance(ex, tk.TclError) else str(ex), parent=self)

    def detect_ip(self, automatic=False):
        if automatic and self.public_host.get():
            return  # Do not overwrite an address entered before the scheduled discovery.
        initial = self.public_host.get()
        self.ip_status.set("正在识别公网出口 IP……")
        def run():
            try:
                self.emit("public_ip", (public_ip(), initial, automatic))
            except Exception as ex:
                self.emit("ip_error", str(ex))
        threading.Thread(target=run, daemon=True).start()

    def prepare_server(self):
        async def prepare():
            identity = await asyncio.to_thread(load_identity)
            agent = ManagedAgent(identity, status=lambda s: self.emit("status", s),
                preview=lambda image: self.show_frame("input", image),
                result_preview=lambda image: self.show_frame("output", image),
                statistics=lambda data: self.emit("stats", data), on_settings=lambda s: self.emit("settings", s))
            return identity, agent
        self.schedule(prepare(), "prepared")

    def refresh_devices(self):
        self.background(list_cameras, "devices")

    def refresh_windows(self):
        self.background(list_windows, "windows")

    def video_settings(self):
        text = self.resolution.get()
        width, height = (720, 1280) if "竖屏" in text else (640, 360) if "360" in text else (1280, 720)
        return VideoSettings(width, height, int(self.fps.get()), int(self.bitrate.get()))

    def local_input(self):
        index = self.camera_box.current()
        devices = [d for d in self.devices if not d.name.startswith("FaceLive Camera")]
        if index < 0 or index > len(devices):
            raise ValueError("请刷新并选择本地摄像头。")
        return "test" if index == 0 else devices[index - 1].id

    def configure_output(self, settings):
        enabled = self.virtual_output.enabled if self.virtual_output else (self.use_input_vcam.get() if self.server else False)
        if self.virtual_output and self.virtual_output.settings == settings:
            return
        if self.virtual_output:
            self.virtual_output.close()
        self.virtual_output = VirtualOutput(settings, 1 if self.server else 0, status=lambda s: self.emit("error", s))
        self.virtual_output.enabled = enabled

    def toggle_virtual(self):
        try:
            self.configure_output(self.video_settings() if not self.network_active() else self.settings)
            self.virtual_output.enabled = not self.virtual_output.enabled
            self.output_status.set("已开启 FaceLive Camera；等待回传时输出黑画面。" if self.virtual_output.enabled else "虚拟摄像头已关闭，推送状态不变。")
            self.vcam_button.configure(text="关闭虚拟摄像头" if self.virtual_output.enabled else "开启虚拟摄像头")
        except Exception as ex:
            self.emit("error", str(ex))

    def toggle_input_virtual(self):
        self.configure_output(self.settings)
        self.virtual_output.enabled = self.use_input_vcam.get()
        self.save_settings()

    def network_active(self):
        return self.network_future is not None and not self.network_future.done()

    def save_settings(self):
        value = {"public_host": self.public_host.get(), "listen_port": self.listen_port.get(), "public_port": self.public_port.get(),
                 "input_vcam": self.use_input_vcam.get()} if self.server else {
                 "address": self.address.get(), "fingerprint": self.fingerprint.get(), "camera": self.camera_box.get(),
                 "work_mode": self.work_mode.get(),
                 "resolution": self.resolution.get(), "fps": self.fps.get(), "bitrate": self.bitrate.get()}
        # The local bearer key stays in memory; the server identity persists separately.
        save_preferences(self.role, value)

    def start_preview(self):
        if self.network_active():
            self.emit("status", "推送中已显示本地输入，无需另开采集。")
            return
        if self.preview_future and not self.preview_future.done():
            self.preview_future.cancel()
            return
        try:
            settings, device = self.video_settings(), self.local_input()
        except Exception as ex:
            self.emit("error", str(ex))
            return
        async def preview():
            self.preview_task = asyncio.current_task()
            self.preview_stop = asyncio.Event()
            try:
                await preview_local(device, settings, self.preview_stop, preview=lambda image: self.show_frame("input", image),
                                    status=lambda s: self.emit("status", s))
            finally:
                self.preview_task = None
                self.emit("preview_done")
        self.preview_future = self.schedule(preview())
        self.preview_button.configure(text="停止本地预览")

    def start(self):
        if self.network_active():
            return
        try:
            self.save_settings()
            if self.server:
                if not self.agent:
                    raise ValueError("服务器身份仍在准备，请稍候。")
                self.update_pairing()
                if not self.generated_code.get():
                    raise ValueError("请先确认对外 IP / 域名和公网端口。")
                port = int(self.listen_port.get())
                if not 1 <= port <= 65535:
                    raise ValueError("监听端口须为 1–65535。")
                async def operation():
                    self.network_stop = asyncio.Event()
                    await serve_direct(self.agent, "0.0.0.0", port, self.network_stop,
                                       on_ready=lambda: self.emit("listening"))
            elif self.relay_mode():
                settings = self.video_settings()
                pair = pairing_from_fields(self.address.get(), self.key.get(), self.fingerprint.get())
                self.settings = settings
                self.configure_output(settings)   # virtual camera size; received frames are fitted to it
                async def operation():
                    if self.preview_task and not self.preview_task.done():
                        old_preview_task = self.preview_task
                        old_preview_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await old_preview_task
                    self.network_stop = asyncio.Event()
                    await connect_viewer(pair, self.network_stop, "none",
                        status=lambda s: self.emit("status", s), preview=lambda image: self.show_frame("output", image),
                        statistics=lambda data: self.emit("stats", data))
            else:
                settings, device = self.video_settings(), self.local_input()
                pair = pairing_from_fields(self.address.get(), self.key.get(), self.fingerprint.get())
                self.settings = settings
                self.configure_output(settings)
                async def operation():
                    if self.preview_task and not self.preview_task.done():
                        old_preview_task = self.preview_task
                        old_preview_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await old_preview_task
                    self.network_stop = asyncio.Event()
                    await connect_direct(pair, device, settings, self.network_stop, "none",
                        status=lambda s: self.emit("status", s), preview=lambda image: self.show_frame("output", image),
                        input_preview=lambda image: self.show_frame("input", image), statistics=lambda data: self.emit("stats", data))
        except Exception as ex:
            messagebox.showerror("无法启动", str(ex), parent=self)
            return
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        async def network():
            try:
                await operation()
            finally:
                self.network_stop = None
                self.emit("network_done")
        self.network_future = self.schedule(network())

    def relay_mode(self):
        return not self.server and self.work_mode.get() == LOCAL_MODES[1]

    def update_mode_labels(self):
        relay = self.relay_mode()
        self.start_button.configure(text="启动接收" if relay else "启动推送")
        self.stop_button.configure(text="停止接收" if relay else "停止推送")
        if not self.network_active():
            self.status.set("填写服务器地址和 Key，点击“启动接收”，再在手机上连接同一服务器。" if relay
                            else "填写服务器地址和 Key，再点击“启动推送”。")

    def stop_work(self):
        if self.network_future and not self.network_future.done():
            if self.network_stop:
                self.engine.loop.call_soon_threadsafe(self.network_stop.set)
            else:
                self.network_future.cancel()
            self.status.set("正在停止网络连接……")
        if not self.server and self.preview_future and not self.preview_future.done():
            self.preview_future.cancel()

    def result_spec(self):
        kind = self.source_type.get()
        if kind == "独立窗口":
            index = self.window_box.current()
            if index < 0 or index >= len(self.windows):
                raise ValueError("请刷新并选择换脸结果窗口。")
            crop = tuple(int(v.get()) for v in self.crop_vars)
            if min(crop) < 0:
                raise ValueError("裁切值不能为负数。")
            return ResultSource("window", window=self.windows[index], method="gdi" if self.method.get().startswith("兼容") else "wgc", crop=crop)
        if kind == "结果虚拟摄像头":
            devices = [d for d in self.devices if d.name != "FaceLive Camera #2"]
            index = self.result_camera.current()
            if index < 0 or index >= len(devices):
                raise ValueError("请选择换脸软件的结果摄像头，不能选择输入 Camera #2。")
            return ResultSource("camera", device=devices[index].id)
        if kind.startswith("原画"):
            if not self.agent or not self.agent.receiving:
                raise ValueError("原画回环需要先收到本地画面。")
            return ResultSource("echo")
        return ResultSource("test")

    def preview_result(self):
        try:
            if not self.agent:
                raise ValueError("服务器尚未准备好。")
            spec = self.result_spec()
            self.capture_ready = False
            self.capture_status.set("正在打开结果来源……")
            self.schedule(self.agent.start_capture(spec), "capture_ready")
        except Exception as ex:
            self.emit("error", str(ex))

    def stop_result(self):
        if self.agent:
            self.schedule(self.agent.stop_capture(), "capture_stopped")

    def toggle_return(self):
        if self.agent:
            desired = not self.agent.return_enabled
            async def change():
                await self.agent.set_return(desired)
                return desired
            self.schedule(change(), "return_changed")

    def show_frame(self, name, image):
        with self.frames_lock:
            self.frames[name] = image.copy()
            self.revisions[name] += 1
        if self.virtual_output and name == ("input" if self.server else "output"):
            self.virtual_output.submit_image(image)

    def diagnose(self):
        def run():
            path = reports_directory() / time.strftime("environment-%Y%m%d-%H%M%S.json")
            save_report(path, report(probe_nvenc=True))
            return f"环境检测已保存到 {path.resolve()}"
        self.background(run, "status")

    def change_view(self):
        if self.single.get():
            self.preview_boxes["input"].grid_remove()
            self.preview_boxes["output"].grid(column=0, columnspan=2)
        else:
            self.preview_boxes["input"].grid()
            self.preview_boxes["output"].grid(column=1, columnspan=1)
        self.displayed = {"input": -1, "output": -1}

    def toggle_logs(self):
        self.logs.grid() if self.log_open.get() else self.logs.grid_remove()

    def clear_preview(self, name, text):
        with self.frames_lock:
            self.frames[name] = None
            self.revisions[name] += 1
        variable = self.input_text if name == "input" else self.result_text
        variable.set(text)
        self.labels[name].configure(image="", textvariable=variable)
        self.preview_rates[name].set("预览显示 — FPS")

    def add_log(self, value):
        self.status.set(value)
        self.logs.configure(state="normal")
        self.logs.insert("end", time.strftime("%H:%M:%S ") + value + "\n")
        if int(self.logs.index("end-1c").split(".")[0]) > 200:
            self.logs.delete("1.0", "50.0")
        self.logs.see("end")
        self.logs.configure(state="disabled")

    def poll(self):
        tick_started = time.perf_counter()
        try:
            for _ in range(100):
                kind, value = self.messages.get_nowait()
                if kind in ("status", "error"):
                    self.add_log(value)
                elif kind == "task_done":
                    self.pending_tasks.discard(value)
                elif kind == "prepared":
                    self.identity, self.agent = value
                    self.key.set(self.identity.connection_key)
                    self.update_pairing()
                    self.configure_output(self.settings)
                    self.add_log("服务器 Key 已准备，点击“复制 Key”交给本地端即可。")
                elif kind == "public_ip":
                    address, initial, automatic = value
                    if not automatic or self.public_host.get() == initial:
                        self.public_host.set(address)
                    self.ip_status.set("检测到公网出口 " + address + "；请与平台入口地址核对。")
                    self.save_settings()
                elif kind == "ip_error":
                    self.ip_status.set(value)
                    self.add_log(value)
                elif kind == "devices":
                    self.devices = value
                    if self.server:
                        labels = [d.name for d in value if d.name != "FaceLive Camera #2"]
                        self.result_camera.configure(values=labels or ["没有可用结果摄像头"])
                        self.result_camera.current(0)
                    else:
                        names = ["测试画面"] + [d.name for d in value if not d.name.startswith("FaceLive Camera")]
                        previous = self.camera_box.get()
                        wanted = previous if previous in names and previous != "测试画面" else self.default_camera_name
                        if wanted not in names:
                            wanted = next((d.name for d in value if "yyanchorvcam" in d.id.lower()), names[0])
                        self.camera_box.configure(values=names)
                        self.camera_box.current(names.index(wanted))
                        if self.pending_preview_camera:
                            device = select_camera(self.pending_preview_camera, value)
                            self.pending_preview_camera = None
                            self.camera_box.current(names.index(device.name))
                            self.start_preview()
                elif kind == "windows":
                    previous = self.window_box.get()
                    self.windows = value
                    names = [w.label for w in value]
                    self.window_box.configure(values=names or ["没有可采集窗口，请先打开结果窗口"])
                    self.window_box.current(names.index(previous) if previous in names else 0)
                elif kind == "listening":
                    if self.config_frame.winfo_manager():
                        self.toggle_config()
                elif kind == "settings":
                    self.settings = value
                    self.configure_output(value)
                    self.add_log(f"本地输入规格：{value.width}×{value.height} / {value.fps} FPS。")
                elif kind == "capture_ready":
                    self.capture_ready = True
                    self.capture_status.set("结果预览已就绪；尚未开始回传。")
                elif kind == "capture_stopped":
                    self.capture_ready = self.returning = False
                    self.capture_status.set("结果采集已停止；接收保持当前状态。")
                    self.return_button.configure(text="开始回传")
                    self.clear_preview("output", "结果采集已停止")
                elif kind == "return_changed":
                    self.returning = value
                    self.return_button.configure(text="停止回传" if value else "开始回传")
                    self.capture_status.set("结果回传中" if value else "结果预览中；尚未回传")
                elif kind == "preview_done":
                    if not self.server:
                        self.preview_button.configure(text="仅本地预览")
                        if not self.network_active():
                            self.clear_preview("input", "本地预览已停止")
                elif kind == "network_done":
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    for variable in self.metric_vars.values():
                        variable.set("—")
                    if self.virtual_output:
                        self.virtual_output.clear()
                    self.clear_preview("input", "等待本地端接入" if self.server else "推送已停止")
                    if not self.server:
                        self.clear_preview("output", "等待下一次连接回传结果")
                    self.add_log("网络已停止；虚拟摄像头及结果预览可单独控制。")
                elif kind == "stats":
                    for key in ("input_fps", "return_fps", "rtt_ms"):
                        number = value.get(key)
                        self.metric_vars[key].set(f"{number:.1f}" if number is not None else "—")
                    self.metric_vars["bandwidth"].set(f"{value['upload_mbps']:.1f} / {value['download_mbps']:.1f}")
        except queue.Empty:
            pass
        except Exception as ex:
            self.add_log(f"界面更新失败：{ex}")
        if self.server and self.agent:
            self.return_button.configure(text="停止回传" if self.agent.return_enabled else "开始回传")
            if self.agent.capture and self.agent.capture.ready and not self.agent.return_enabled:
                self.capture_status.set("结果预览中；尚未回传。")
            if self.agent.capture and self.agent.capture.error:
                self.capture_status.set("结果采集失败，请重新预览；原画接收仍可继续。")
                self.capture_ready = False
        if self.virtual_output and not self.server:
            self.vcam_button.configure(text="关闭虚拟摄像头" if self.virtual_output.enabled else "开启虚拟摄像头")
        for name, label in self.labels.items():
            if name == "input" and self.single.get():
                continue
            with self.frames_lock:
                image, revision = self.frames[name], self.revisions[name]
            size = max(1, label.winfo_width()), max(1, label.winfo_height())
            if image is not None and (revision != self.displayed[name] or getattr(label, "_last_size", None) != size):
                fitted = fit_bgr(image, *size)
                rgb = cv2.cvtColor(fitted, cv2.COLOR_BGR2RGB)
                ppm = f"P6\n{size[0]} {size[1]}\n255\n".encode("ascii") + rgb.tobytes()
                if name not in self.images:
                    self.images[name] = tk.PhotoImage(master=self, data=ppm, format="PPM")
                else:
                    self.images[name].configure(data=ppm, format="PPM")
                # Reuse the same Tk image; do not delete/reallocate it every frame.
                label.configure(image=self.images[name], textvariable="")
                label._last_size = size
                self.displayed[name] = revision
                if revision != self.presented_revisions[name]:
                    self.preview_counts[name] += 1
                    self.preview_times[name].append(time.perf_counter())
                    self.presented_revisions[name] = revision
        now = time.perf_counter()
        sample_duration = now - self.preview_sample_time
        if sample_duration >= 1:
            for name in self.preview_counts:
                fps = (self.preview_counts[name] - self.preview_sample_counts[name]) / sample_duration
                self.preview_rates[name].set(f"预览显示 {fps:.1f} FPS")
            self.preview_sample_counts = dict(self.preview_counts)
            self.preview_sample_time = now
        if not self.closing:
            elapsed_ms = (time.perf_counter() - tick_started) * 1000
            self.after(max(1, math.ceil(1000 / PREVIEW_POLL_HZ - elapsed_ms)), self.poll)

    def close_window(self):
        if self.closing:
            return
        self.closing = True
        with contextlib.suppress(Exception):
            self.save_settings()
        self.withdraw()
        async def cleanup():
            # Cancel network/preview first; then release the result source and its child.
            if self.network_future:
                self.network_future.cancel()
            if self.preview_future:
                self.preview_future.cancel()
            for item in list(self.pending_tasks):
                if item not in (self.network_future, self.preview_future):
                    item.cancel()
            if self.agent:
                await self.agent.stop_capture()
            if self.virtual_output:
                await asyncio.to_thread(self.virtual_output.close)
        future = self.engine.submit(cleanup())
        deadline = time.monotonic() + 15
        def check():
            if future.done() or time.monotonic() > deadline:
                self.engine.loop.call_soon_threadsafe(self.engine.loop.stop)
                self.destroy()
            else:
                self.after(100, check)
        self.after(100, check)


def launch(role="local", preview_camera=None, portrait=False):
    Application(role, preview_camera, portrait).mainloop()
