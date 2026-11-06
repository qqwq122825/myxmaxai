"""Play phone microphone PCM through the Windows default audio output."""
import ctypes as ct
from ctypes import wintypes as wt
import threading

AUDIO_MAGIC = b"RGA1"
AUDIO_RATE = 48000
MAX_PCM_BYTES = 1920  # 20 ms of mono, signed 16-bit little-endian PCM.


class WaveFormat(ct.Structure):
    _pack_ = 1
    _fields_ = [("format", wt.WORD), ("channels", wt.WORD), ("rate", wt.DWORD),
                ("bytes_per_second", wt.DWORD), ("alignment", wt.WORD),
                ("bits", wt.WORD), ("extra", wt.WORD)]


class WaveHeader(ct.Structure):
    _fields_ = [("data", ct.c_void_p), ("length", wt.DWORD), ("recorded", wt.DWORD),
                ("user", ct.c_size_t), ("flags", wt.DWORD), ("loops", wt.DWORD),
                ("next", ct.c_void_p), ("reserved", ct.c_size_t)]


class Speaker:
    """Keep at most 60 ms queued. Drop new chunks if Windows falls behind."""
    def __init__(self):
        self.api = ct.WinDLL("winmm")
        self.handle = wt.HANDLE()
        self.pending = []
        self.api.waveOutOpen.argtypes = [ct.POINTER(wt.HANDLE), wt.UINT,
                                        ct.POINTER(WaveFormat), ct.c_size_t, ct.c_size_t, wt.DWORD]
        for name in ("waveOutPrepareHeader", "waveOutWrite", "waveOutUnprepareHeader"):
            getattr(self.api, name).argtypes = [wt.HANDLE, ct.POINTER(WaveHeader), wt.UINT]
        self.api.waveOutReset.argtypes = [wt.HANDLE]
        self.api.waveOutClose.argtypes = [wt.HANDLE]
        fmt = WaveFormat(1, 1, AUDIO_RATE, AUDIO_RATE * 2, 2, 16, 0)
        if self.api.waveOutOpen(ct.byref(self.handle), 0xffffffff, ct.byref(fmt), 0, 0, 0):
            raise OSError("无法打开 Windows 默认音频输出")

    def _reap(self):
        for entry in self.pending[:]:
            _, header = entry
            if header.flags & 1:  # WHDR_DONE
                if not self.api.waveOutUnprepareHeader(self.handle, ct.byref(header), ct.sizeof(header)):
                    self.pending.remove(entry)

    def write(self, pcm):
        self._reap()
        if len(self.pending) >= 3:
            return
        data = ct.create_string_buffer(pcm)
        header = WaveHeader(ct.addressof(data), len(pcm))
        if self.api.waveOutPrepareHeader(self.handle, ct.byref(header), ct.sizeof(header)):
            raise OSError("音频缓冲区初始化失败")
        if self.api.waveOutWrite(self.handle, ct.byref(header), ct.sizeof(header)):
            self.api.waveOutUnprepareHeader(self.handle, ct.byref(header), ct.sizeof(header))
            raise OSError("音频播放失败")
        self.pending.append((data, header))

    def reset(self):
        self.api.waveOutReset(self.handle)
        self._reap()

    def close(self):
        self.reset()
        self.api.waveOutClose(self.handle)


class AudioMonitor:
    def __init__(self, on_error, speaker_factory=Speaker):
        self.on_error = on_error
        self.speaker_factory = speaker_factory
        self.lock = threading.Lock()
        self.speaker = None

    def start(self):
        with self.lock:
            if self.speaker is None:
                self.speaker = self.speaker_factory()

    def receive(self, pcm):
        with self.lock:
            if self.speaker is None:
                return
            try:
                self.speaker.write(pcm)
            except OSError as ex:
                self.speaker.close()
                self.speaker = None
                self.on_error(str(ex))

    def reset(self):
        with self.lock:
            if self.speaker:
                self.speaker.reset()

    def stop(self):
        with self.lock:
            if self.speaker:
                self.speaker.close()
                self.speaker = None
