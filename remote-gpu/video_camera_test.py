"""Windowed entry point, also used by the frozen installer subprocess."""
from pathlib import Path
import sys


def main():
    if "--install-driver" in sys.argv:
        try:
            from rgpu.video_test import install_driver
            install_driver()
        except Exception:
            import os
            import traceback
            report = Path(os.environ["LOCALAPPDATA"]) / "FaceLiveVideoTest" / "install-error.txt"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(traceback.format_exc(), encoding="utf-8")
            return 1
        return 0
    from rgpu.video_test_gui import VideoTestWindow
    VideoTestWindow().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
