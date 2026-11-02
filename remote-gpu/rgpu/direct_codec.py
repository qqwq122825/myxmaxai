"""H.264 frames carried over an ordered, encrypted, duplex WebSocket."""
from __future__ import annotations

from fractions import Fraction
import struct

import av

from .config import VideoSettings
from .framelock import FRAME_CONVERT_LOCK

HEADER = struct.Struct("!4sqHH")
MAX_MESSAGE = 4 * 1024 * 1024
TIME_BASE = Fraction(1, 90_000)


class H264Encoder:
    def __init__(self, settings: VideoSettings):
        self.settings = settings
        self.codec = av.CodecContext.create("libx264", "w")
        self.codec.width, self.codec.height = settings.width, settings.height
        self.codec.pix_fmt = "yuv420p"
        self.codec.time_base = TIME_BASE
        self.codec.framerate = Fraction(settings.fps, 1)
        self.codec.bit_rate = settings.bitrate_kbps * 1000
        self.codec.gop_size = settings.fps
        self.codec.max_b_frames = 0
        self.codec.thread_count = 2
        self.codec.options = {"preset": "veryfast", "tune": "zerolatency", "profile": "baseline",
                              "x264-params": f"repeat-headers=1:scenecut=0:keyint={settings.fps}"}
        self.last_pts = -1

    def encode(self, frame):
        s = self.settings
        if frame.width != s.width or frame.height != s.height:
            raise ValueError("视频尺寸与直连会话协商值不一致。")
        pts = int(frame.pts * frame.time_base / TIME_BASE) if frame.pts is not None and frame.time_base else self.last_pts + 90_000 // s.fps
        pts = max(self.last_pts + 1, pts)
        with FRAME_CONVERT_LOCK:   # a preview thread may be converting this same frame
            image = frame.reformat(format="yuv420p")
        image.pts, image.time_base = pts, TIME_BASE
        self.last_pts = pts
        result = []
        for packet in self.codec.encode(image):
            message = HEADER.pack(b"RGV1", packet.pts if packet.pts is not None else pts, s.width, s.height) + bytes(packet)
            if len(message) > MAX_MESSAGE:
                raise ValueError("编码帧超过直连消息限制。")
            result.append(message)
        return result


class H264Decoder:
    def __init__(self, settings):
        self.settings = settings
        self.codec = av.CodecContext.create("h264", "r")
        self.codec.thread_count = 2
        self.last_pts = -1

    def decode(self, message):
        if not HEADER.size < len(message) <= MAX_MESSAGE:
            raise ValueError("视频消息长度不正确。")
        magic, pts, width, height = HEADER.unpack_from(message)
        if magic != b"RGV1" or (width, height) != (self.settings.width, self.settings.height):
            raise ValueError("视频消息格式或尺寸不正确。")
        if pts < 0 or pts <= self.last_pts:
            raise ValueError("视频消息时间戳倒退。")
        packet = av.Packet(message[HEADER.size:])
        packet.pts = packet.dts = pts
        packet.time_base = TIME_BASE
        frames = list(self.codec.decode(packet))
        for frame in frames:
            if (frame.width, frame.height) != (width, height):
                raise ValueError("解码后的画面尺寸与会话不一致。")
            frame.pts, frame.time_base = pts, TIME_BASE
        self.last_pts = pts
        return frames
