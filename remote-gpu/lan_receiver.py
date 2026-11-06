"""Windowed, independently packaged LAN receiver entry point."""
import sys


def main():
    if "--install-lan-camera" in sys.argv:
        try:
            from rgpu.lan_camera import install_driver
            install_driver()
            return 0
        except Exception:
            return 1  # Detailed reason is saved by the installer.
    if "--selftest" in sys.argv:
        from rgpu.lan_selftest import main as selftest
        return selftest(sys.argv[sys.argv.index("--selftest") + 1:])
    from rgpu.lan_gui import LanWindow
    LanWindow().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
