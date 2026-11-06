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
    """Windows waveOut output. Queueing / timing is decided by AudioMonitor's jitter buffer."""
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

    def queued(self):
        """Blocks handed to Windows that have not finished playing."""
        self._reap()
        return len(self.pending)

    def write(self, pcm):
        self._reap()
        data = ct.create_string_buffer(pcm, len(pcm))
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


BYTES_PER_SECOND = AUDIO_RATE * 2
BLOCK_BYTES = BYTES_PER_SECOND * 40 // 1000        # hand Windows 40 ms at a time
PREBUFFER_BYTES = BYTES_PER_SECOND * 120 // 1000   # collect 120 ms before (re)starting playback
MAX_LATENCY_BYTES = BYTES_PER_SECOND * 300 // 1000  # never let the delay grow beyond ~300 ms
MAX_BLOCKS = MAX_LATENCY_BYTES // BLOCK_BYTES + 1


class AudioMonitor:
    """Jitter buffer between Wi-Fi and the sound card.

    Wi-Fi delivers the 20 ms phone packets in bursts. Playing each packet the moment it arrives with only
    ~60 ms queued makes the card run dry between bursts (clicks / crackle) and drops packets inside a burst
    (choppy). Instead: wait for 120 ms, then keep the card fed in 40 ms blocks; after a gap, refill to
    120 ms before playing again; if delay builds up past ~300 ms, skip the oldest audio to catch up.
    """
    def __init__(self, on_error, speaker_factory=Speaker):
        self.on_error = on_error
        self.speaker_factory = speaker_factory
        self.lock = threading.Lock()
        self.speaker = None
        self.buffer = bytearray()
        self.priming = True
        self.underruns = 0
        self.skipped_bytes = 0

    def start(self):
        with self.lock:
            if self.speaker is None:
                self.speaker = self.speaker_factory()
                self.buffer.clear()
                self.priming = True

    def _queued(self):
        queued = getattr(self.speaker, "queued", None)
        return queued() if queued else 0

    def receive(self, pcm):
        with self.lock:
            if self.speaker is None:
                return
            try:
                self.buffer += pcm
                queued = self._queued()
                if queued == 0 and not self.priming:
                    self.priming = True          # the card ran dry: refill before resuming
                    self.underruns += 1
                if self.priming:
                    if len(self.buffer) < PREBUFFER_BYTES:
                        return
                    self.priming = False
                excess = queued * BLOCK_BYTES + len(self.buffer) - MAX_LATENCY_BYTES
                if excess > 0:                   # a big burst after a stall: drop the oldest, keep sync
                    excess = min(excess + excess % 2, len(self.buffer))
                    del self.buffer[:excess]
                    self.skipped_bytes += excess
                while len(self.buffer) >= BLOCK_BYTES and queued < MAX_BLOCKS:
                    self.speaker.write(bytes(self.buffer[:BLOCK_BYTES]))
                    del self.buffer[:BLOCK_BYTES]
                    queued += 1
            except OSError as ex:
                self.speaker.close()
                self.speaker = None
                self.on_error(str(ex))

    def reset(self):
        with self.lock:
            self.buffer.clear()
            self.priming = True
            if self.speaker:
                self.speaker.reset()

    def stop(self):
        with self.lock:
            self.buffer.clear()
            self.priming = True
            if self.speaker:
                self.speaker.close()
                self.speaker = None
