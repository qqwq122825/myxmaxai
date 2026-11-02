"""One lock around PyAV pixel-format conversion of frames that may be shared between threads.

The same decoded / captured frame object is handed to more than one consumer (for example the
H.264 encoder thread and a preview / virtual-camera thread). Converting one AVFrame from two
threads at the same time intermittently crashed the process (SIGSEGV inside libswscale), so
conversions of shared frames take turns. Each conversion is a few milliseconds at 720p.
"""
import threading

FRAME_CONVERT_LOCK = threading.Lock()
