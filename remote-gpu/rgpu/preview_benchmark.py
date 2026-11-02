"""Measure actual Tk image updates in the visible desktop UI; synthetic video only."""
from __future__ import annotations

import asyncio
import time

import numpy as np

from .config import VideoSettings
from .media import FrameSink, PatternTrack


def run_preview_benchmark(seconds=5, portrait=False, legacy=False):
    from . import gui, __version__
    if not 2 <= seconds <= 30:
        raise ValueError("预览测试时长须为 2–30 秒。")
    # This is an isolated diagnostic process: never use/persist the user's settings,
    # open a real camera, write virtual output, or connect to their GPU server.
    gui.load_preferences = lambda _: {}
    gui.save_preferences = lambda *_: None
    gui.list_cameras = lambda: []
    gui.PREVIEW_POLL_HZ = 10 if legacy else 60
    app = gui.Application("local", portrait=portrait)
    app.title("FaceLive Remote · 双画面预览性能测试（合成图）")
    settings = VideoSettings(720, 1280) if portrait else VideoSettings()
    produced = [0]
    measured = {}
    app.status.set("合成视频预览测试：两路动态画面；未使用摄像头、网络或虚拟摄像头。")

    async def source():
        pattern = PatternTrack(settings)
        sinks = [FrameSink(settings, "none", lambda image, side=side: app.show_frame(side, image))
                 for side in ("input", "output")]
        try:
            for sink in sinks:
                await sink.wait_ready()
            while True:
                frame = await pattern.recv()
                produced[0] += 1
                for sink in sinks:
                    sink.submit(frame)
        finally:
            pattern.stop()
            for sink in sinks:
                await sink.close()

    def finish():
        elapsed = time.perf_counter() - measured["started"]
        result = {"version": __version__, "legacy_10hz_comparison": legacy,
                  "source_fps": (produced[0] - measured["source_count"]) / elapsed,
                  "resolution": [settings.width, settings.height], "seconds": elapsed,
                  "preview_fps": {}, "preview_gap_p95_ms": {}, "preview_gap_max_ms": {},
                  "scope": "visible Tk image updates, synthetic source; not monitor scanout or end-to-end latency",
                  "network_used": False, "virtual_camera_written": False}
        for name in ("input", "output"):
            result["preview_fps"][name] = (app.preview_counts[name] - measured["counts"][name]) / elapsed
            timestamps = [t for t in app.preview_times[name] if t >= measured["started"]]
            gaps = np.diff(timestamps) * 1000
            result["preview_gap_p95_ms"][name] = float(np.percentile(gaps, 95)) if len(gaps) else None
            result["preview_gap_max_ms"][name] = float(max(gaps)) if len(gaps) else None
        result["ok"] = (result["source_fps"] >= 25 and
                        min(result["preview_fps"].values()) >= (7 if legacy else 25))
        measured["result"] = result
        app.close_window()

    def begin_sample():
        measured.update(started=time.perf_counter(), source_count=produced[0], counts=dict(app.preview_counts))
        app.after(round(seconds * 1000), finish)

    app.schedule(source())
    app.after(1200, begin_sample)
    app.mainloop()
    return measured.get("result", {"ok": False, "error": "预览性能测试提前结束。"})
