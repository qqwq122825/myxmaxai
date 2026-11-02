"""Small independent window: choose a video, start camera, stop camera."""
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import cv2

from .video_test import (CAMERA_NAME, LoopingVideo, VideoPlayback, driver_installed,
                         fit_frame, install_elevated)

SIZES = {"720P 横屏 · 1280×720": (1280, 720), "720P 竖屏 · 720×1280": (720, 1280),
         "1080P 横屏 · 1920×1080": (1920, 1080), "1080P 竖屏 · 1080×1920": (1080, 1920)}


class VideoTestWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("视频虚拟摄像头测试 · " + CAMERA_NAME + " · 1.0.1")
        self.geometry("840x660")
        self.minsize(720, 600)
        self.configure(bg="#f3f5f9")
        self.option_add("*Font", "{Microsoft YaHei UI} 10")
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TButton", padding=(14, 10))
        style.configure("Start.TButton", foreground="white", background="#4264ee")
        self.events = queue.Queue()
        self.preview_lock = threading.Lock()
        self.latest, self.revision, self.displayed = None, 0, -1
        self.path = None
        self.playback = None
        self.busy = self.closing = self.stopping = False
        self.status = tk.StringVar(value="添加一个视频，然后启动虚拟摄像头。")
        self.file_label = tk.StringVar(value="尚未添加视频")
        self.size = tk.StringVar(value=next(iter(SIZES)))
        self.driver = tk.StringVar(value="摄像头已安装" if driver_installed() else "首次启动会请求管理员权限，安装测试摄像头")

        body = tk.Frame(self, bg="#f3f5f9", padx=24, pady=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        tk.Label(body, text="视频虚拟摄像头测试", bg="#f3f5f9", fg="#25354b",
                 font=("Microsoft YaHei UI", 20, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(body, text=f"输出设备：{CAMERA_NAME}    ·    视频自动循环播放", bg="#f3f5f9",
                 fg="#65758b").grid(row=1, column=0, sticky="w", pady=(6, 14))
        # Only this row absorbs window resizing. A placed image cannot increase
        # its parent's requested size and crowd the controls out of the window.
        preview_area = tk.Frame(body, bg="#101827", width=1, height=1)
        preview_area.grid(row=2, column=0, sticky="nsew")
        self.preview = tk.Label(preview_area, bg="#101827", fg="#98a8c0", borderwidth=0,
                                highlightthickness=0, text="添加视频后在这里预览")
        self.preview.place(x=0, y=0, relwidth=1, relheight=1)
        self.preview.bind("<Configure>", lambda _: setattr(self, "displayed", -1))
        controls = tk.Frame(body, bg="#f3f5f9")
        controls.grid(row=3, column=0, sticky="ew")
        tk.Label(controls, textvariable=self.file_label, bg="#f3f5f9", fg="#25354b",
                 anchor="w", wraplength=740, justify="left").pack(fill="x", pady=(10, 8))
        options = tk.Frame(controls, bg="#f3f5f9")
        options.pack(fill="x")
        tk.Label(options, text="输出画面", bg="#f3f5f9", fg="#25354b").pack(side="left", padx=(0, 10))
        self.size_box = ttk.Combobox(options, textvariable=self.size, values=list(SIZES), state="readonly", width=28)
        self.size_box.pack(side="left")
        tk.Label(options, text="等比缩放，保留完整画面", bg="#f3f5f9", fg="#65758b").pack(side="left", padx=12)
        actions = tk.Frame(controls, bg="#f3f5f9")
        actions.pack(fill="x", pady=14)
        self.add_button = ttk.Button(actions, text="添加视频", command=self.choose_video)
        self.add_button.pack(side="left")
        self.start_button = ttk.Button(actions, text="启动虚拟摄像头", command=self.start_camera, style="Start.TButton")
        self.start_button.pack(side="left", padx=12)
        self.stop_button = ttk.Button(actions, text="关闭虚拟摄像头", command=self.stop_camera)
        self.stop_button.pack(side="left")
        tk.Label(controls, textvariable=self.status, bg="#f3f5f9", fg="#25354b", anchor="w",
                 justify="left", wraplength=740).pack(fill="x")
        tk.Label(controls, textvariable=self.driver, bg="#f3f5f9", fg="#65758b", anchor="w").pack(fill="x", pady=(6, 0))
        tk.Label(controls, text=f"在 OBS、直播伴侣等软件中选择“{CAMERA_NAME}”。仅输出视频画面。",
                 bg="#f3f5f9", fg="#65758b", anchor="w").pack(fill="x", pady=(4, 0))
        self.protocol("WM_DELETE_WINDOW", self.close_window)
        self.update_buttons()
        self.after(50, self.poll)

    def update_buttons(self):
        active = self.playback is not None
        self.add_button.configure(state="disabled" if active or self.busy else "normal")
        self.start_button.configure(state="normal" if self.path and not active and not self.busy else "disabled")
        self.stop_button.configure(state="normal" if active and not self.stopping else "disabled")
        self.size_box.configure(state="disabled" if active or self.busy else "readonly")

    def set_frame(self, image):
        with self.preview_lock:
            self.latest = image
            self.revision += 1

    def choose_video(self):
        path = filedialog.askopenfilename(parent=self, title="添加循环播放的视频",
            filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.wmv *.ts"), ("所有文件", "*.*")])
        if path:
            self.load_video(path)

    def load_video(self, path):
        if self.busy or self.playback:
            return
        self.busy = True
        self.status.set("正在读取视频……")
        self.update_buttons()
        def load():
            source = None
            try:
                source = LoopingVideo(path)
                image = source.read()
                self.events.put(("loaded", (path, image, source.fps)))
            except Exception as ex:
                self.events.put(("error", str(ex)))
            finally:
                if source:
                    source.close()
        threading.Thread(target=load, daemon=True, name="Load test video").start()

    def start_camera(self):
        if not self.path or self.busy or self.playback:
            return
        if not driver_installed():
            self.busy = True
            self.status.set("正在安装摄像头，请在系统提示中允许安装……")
            self.update_buttons()
            def install():
                try:
                    install_elevated()
                    self.events.put(("installed", None))
                except Exception as ex:
                    self.events.put(("error", str(ex)))
            threading.Thread(target=install, daemon=True, name="Install test camera").start()
            return
        width, height = SIZES[self.size.get()]
        self.playback = VideoPlayback(self.path, width, height, frame=self.set_frame,
                                     status=lambda message: self.events.put(("status", message)))
        self.status.set("正在启动虚拟摄像头……")
        self.stopping = False
        self.playback.start()
        self.update_buttons()

    def stop_camera(self):
        if self.playback:
            self.stopping = True
            self.playback.stop()
            self.status.set("正在关闭虚拟摄像头……")
            self.update_buttons()

    def close_window(self):
        self.closing = True
        self.stop_camera()
        if not self.playback and not self.busy:
            self.destroy()

    def poll(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "loaded":
                path, image, fps = value
                self.path, self.busy = path, False
                h, w = image.shape[:2]
                self.file_label.set(f"{Path(path).name}  ·  {w}×{h}  ·  {fps:.2f} FPS")
                self.size.set(list(SIZES)[1] if h > w else next(iter(SIZES)))
                self.set_frame(image)
                self.status.set("视频已添加，点击启动后循环播放。")
            elif kind == "installed":
                self.busy = False
                self.driver.set("摄像头已安装")
                if not self.closing:
                    self.start_camera()
            elif kind == "error":
                self.busy = False
                self.status.set(str(value))
            elif kind == "status" and not self.stopping:
                self.status.set(value)
            self.update_buttons()
        if self.closing and not self.busy and not self.playback:
            self.destroy()
            return
        if self.playback and not self.playback.thread.is_alive():
            error = self.playback.error
            self.playback = None
            self.stopping = False
            self.status.set("输出失败：" + error if error else "虚拟摄像头已关闭。")
            self.update_buttons()
            if self.closing:
                self.destroy()
                return
        with self.preview_lock:
            image, revision = self.latest, self.revision
        if image is not None and revision != self.displayed:
            w, h = max(1, self.preview.winfo_width()), max(1, self.preview.winfo_height())
            display = fit_frame(image, w, h)
            ok, encoded = cv2.imencode(".png", display)
            if ok:
                self.photo = tk.PhotoImage(data=encoded.tobytes())
                self.preview.configure(image=self.photo, text="")
                self.displayed = revision
        self.after(50, self.poll)
