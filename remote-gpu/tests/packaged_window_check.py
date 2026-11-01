"""Verify frozen multiprocessing capture using an owned test window, never a user window."""
import ctypes as ct
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk


def main():
    exe = Path(sys.argv[1]).resolve()
    root = tk.Tk()
    root.title("RemoteGpu packaged window capture test")
    root.configure(bg="#00bb33")
    root.geometry("640x420+60+60")
    root.update()
    user = ct.WinDLL("user32", use_last_error=True)
    user.GetParent.restype, user.GetParent.argtypes = ct.c_void_p, [ct.c_void_p]
    hwnd = int(user.GetParent(root.winfo_id()) or root.winfo_id())
    results = []
    try:
        for method in ("wgc", "gdi"):
            report = Path(f"reports/packaged-window-{method}-030.json").resolve()
            child = subprocess.Popen([str(exe), "window-probe", "--hwnd", str(hwnd), "--pid", str(os.getpid()),
                "--method", method, "--seconds", "3", "--report", str(report)], creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 25
            while child.poll() is None and time.monotonic() < deadline:
                root.update()
                time.sleep(.02)
            if child.poll() is None:
                child.terminate()
                child.wait(5)
                raise TimeoutError("Frozen window capture did not exit")
            if child.returncode:
                raise RuntimeError(f"Frozen {method} capture returned {child.returncode}")
            value = json.loads(report.read_text(encoding="utf-8"))
            assert value["ok"] and value["frames"] > 25
            assert value["center_bgr"][1] > 120 and value["center_bgr"][2] < 80
            results.append(value)
    finally:
        root.destroy()
    print(json.dumps(results))


if __name__ == "__main__":
    main()
