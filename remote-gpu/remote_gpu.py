from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import ssl
import sys

from rgpu.config import VideoSettings, private_address, token_file
from rgpu import __version__
from rgpu.doctor import report, save_report
from rgpu.paths import reports_directory
from rgpu.transport import Agent, connect_client, serve


def console(message):
    if sys.stdout:
        print(message, flush=True)


def parser():
    p = argparse.ArgumentParser(description=f"远程 GPU 视频桥接 {__version__}：双端传输，不修改第三方换脸软件。")
    sub = p.add_subparsers(dest="command")
    gui = sub.add_parser("gui", help="打开图形界面")
    gui.add_argument("--role", choices=("local", "agent"), default="local")
    gui.add_argument("--smoke-report", help=argparse.SUPPRESS)
    gui.add_argument("--preview-camera", help="启动后直接本地预览指定设备；yy 表示 YY 开播")
    gui.add_argument("--portrait", action="store_true", help="本地预览使用 720×1280 竖屏")
    render_test = sub.add_parser("preview-benchmark", help="用两路合成图测量界面实际预览绘制帧率")
    render_test.add_argument("--seconds", type=int, default=5)
    render_test.add_argument("--portrait", action="store_true")
    render_test.add_argument("--legacy", action="store_true", help=argparse.SUPPRESS)
    render_test.add_argument("--report", default="reports/preview-benchmark.json")
    doctor = sub.add_parser("doctor", help="检测 Windows / GPU / 摄像头 / 编码器")
    doctor.add_argument("--probe-nvenc", action="store_true")
    doctor.add_argument("--report", default="reports/environment.json")
    camtest = sub.add_parser("camera-test", help="虚拟摄像头自检：写入测试画面并用 DirectShow / OpenCV / Media Foundation 读回")
    camtest.add_argument("--channel", type=int, default=1, help="1 = 服务器给换脸软件的 Camera #2（默认）；0 = 本地直播输出")
    camtest.add_argument("--report", default="reports/camera-selftest.json")
    test = sub.add_parser("selftest", help="不使用摄像头的本机 WebRTC 回环")
    test.add_argument("--seconds", type=int, default=6)
    test.add_argument("--report", default="reports/selftest.json")
    cameras = sub.add_parser("cameras", help="列出 DirectShow 摄像头及稳定设备 ID")
    sub.add_parser("windows", help="列出本机可选择的窗口及 HWND / PID")
    window_probe = sub.add_parser("window-probe", help="仅本地测试指定结果窗口采集")
    window_probe.add_argument("--hwnd", required=True, type=int)
    window_probe.add_argument("--pid", required=True, type=int)
    window_probe.add_argument("--method", choices=("wgc", "gdi"), default="wgc")
    window_probe.add_argument("--seconds", type=int, default=4)
    window_probe.add_argument("--report", default="reports/window-probe.json")
    probe = sub.add_parser("probe", help="仅本地测试摄像头，不联网、不写虚拟摄像头")
    probe.add_argument("--camera", default="yy")
    probe.add_argument("--width", type=int, default=1280)
    probe.add_argument("--height", type=int, default=720)
    probe.add_argument("--fps", type=int, default=30)
    probe.add_argument("--seconds", type=int, default=5)
    probe.add_argument("--report", default="reports/camera-probe.json")
    direct_test = sub.add_parser("direct-selftest", help="单端口加密直连回环，不需要账号或组网工具")
    direct_test.add_argument("--seconds", type=int, default=6)
    direct_test.add_argument("--camera", default="test", help="test、yy 或设备名称")
    direct_test.add_argument("--portrait", action="store_true")
    direct_test.add_argument("--report", default="reports/direct-selftest.json")
    direct_agent = sub.add_parser("direct-agent", help="免账号公网直连远程端")
    direct_agent.add_argument("--host", default="0.0.0.0")
    direct_agent.add_argument("--public-host", required=True)
    direct_agent.add_argument("--port", type=int, default=8766)
    direct_agent.add_argument("--public-port", type=int)
    direct_agent.add_argument("--mode", choices=("echo", "bridge"), default="echo")
    direct_agent.add_argument("--output-camera", default="")
    direct_agent.add_argument("--pairing-file", default="server.pairing")
    direct_client = sub.add_parser("direct-client", help="使用服务器配对文件连接，无需账号")
    direct_client.add_argument("--pairing-file", required=True)
    direct_client.add_argument("--camera", default="test")
    direct_client.add_argument("--portrait", action="store_true")
    direct_client.add_argument("--sink", choices=("unity:0", "none"), default="unity:0")
    agent = sub.add_parser("agent", help="启动远程接收 / 发送端")
    agent.add_argument("--host", default="127.0.0.1")
    agent.add_argument("--port", type=int, default=8765)
    agent.add_argument("--mode", choices=("echo", "bridge"), default="echo")
    agent.add_argument("--output-camera", default="")
    agent.add_argument("--token-file", default="server.access-token")
    agent.add_argument("--ice-config")
    agent.add_argument("--cert")
    agent.add_argument("--key")
    client = sub.add_parser("client", help="启动本地摄像头上传 / 回传接收")
    client.add_argument("--url", required=True)
    client.add_argument("--camera", default="test", help="test 或 cameras 命令列出的设备 ID")
    client.add_argument("--token-file")
    client.add_argument("--width", type=int, default=1280)
    client.add_argument("--height", type=int, default=720)
    client.add_argument("--fps", type=int, default=30)
    client.add_argument("--bitrate", type=int, default=4000)
    client.add_argument("--sink", choices=("unity:0", "none"), default="unity:0")
    client.add_argument("--ice-config")
    return p


async def async_main(args):
    if args.command == "window-probe":
        from rgpu.window_capture import WindowDevice, WindowTrack
        import time
        if not 1 <= args.seconds <= 120:
            raise ValueError("测试时长须为 1–120 秒。")
        source = WindowTrack(WindowDevice(args.hwnd, args.pid, "Selected result"), VideoSettings(), args.method)
        count, colors = 0, []
        first = None
        try:
            while True:
                frame = await source.recv()
                now = time.monotonic()
                if first is None:
                    first = now
                count += 1
                image = frame.to_ndarray(format="bgr24")
                colors.append(image[frame.height // 3:frame.height * 2 // 3,
                                    frame.width // 3:frame.width * 2 // 3].mean(axis=(0, 1)).tolist())
                if now - first >= args.seconds:
                    break
            save_report(args.report, {"ok": True, "frames": count, "fps": (count - 1) / (now - first),
                                      "method": args.method, "size": [frame.width, frame.height],
                                      "center_bgr": colors[-1], "network_used": False})
        finally:
            source.stop()
            await source.wait_closed()
        return
    if args.command == "direct-selftest":
        from rgpu.direct_selftest import run_direct_selftest
        from rgpu.media import select_camera
        if not 1 <= args.seconds <= 120:
            raise ValueError("回环时长须为 1–120 秒。")
        device = "test" if args.camera == "test" else select_camera(args.camera).id
        settings = VideoSettings(720, 1280) if args.portrait else VideoSettings()
        result = await run_direct_selftest(args.seconds, device, settings, status=console)
        save_report(args.report, result)
        console(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "direct-agent":
        from rgpu.direct import DirectAgent, serve_direct
        from rgpu.pairing import load_identity
        identity = load_identity()
        pairing = identity.pairing(args.public_host, args.public_port or args.port)
        agent = DirectAgent(identity, args.mode, args.output_camera, status=console)
        def ready():
            path = Path(args.pairing_file)
            path.write_text(pairing.encode(), encoding="ascii")
            path.chmod(0o600)
            console(f"配对码已保存到 {path.resolve()}，本地端导入此文件即可。")
        await serve_direct(agent, args.host, args.port, asyncio.Event(), on_ready=ready)
        return
    if args.command == "direct-client":
        from rgpu.direct import connect_direct
        from rgpu.media import select_camera
        device = "test" if args.camera == "test" else select_camera(args.camera).id
        settings = VideoSettings(720, 1280) if args.portrait else VideoSettings()
        await connect_direct(Path(args.pairing_file).read_text(encoding="ascii"), device, settings,
                             asyncio.Event(), args.sink, status=console)
        return
    if args.command == "probe":
        from rgpu.media import select_camera
        from rgpu.preview import preview_local
        device = select_camera(args.camera)
        if not 1 <= args.seconds <= 120:
            raise ValueError("本地采集测试时长须为 1–120 秒。")
        result = await preview_local(device.id, VideoSettings(args.width, args.height, args.fps),
                                     asyncio.Event(), seconds=args.seconds, status=console)
        result["device"] = device.name
        save_report(args.report, result)
        console(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "selftest":
        from rgpu.selftest import run_selftest
        try:
            result = await run_selftest(args.seconds, status=console)
        except Exception as ex:
            save_report(args.report, {"ok": False, "error": str(ex)})
            raise
        save_report(args.report, result)
        console(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "agent":
        tls = None
        if args.cert and args.key:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(args.cert, args.key)
        elif not private_address(args.host):
            raise ValueError("无 TLS 时只允许绑定具体的本机 / 局域网 / Tailscale IP；请不要绑定 0.0.0.0。")
        token = os.environ.get("REMOTE_GPU_TOKEN") or token_file(Path(args.token_file))
        console(f"密钥位于 {Path(args.token_file).resolve()}；在本地界面填入其内容，不要分享给他人。")
        agent = Agent(token, args.mode, args.output_camera, args.ice_config, status=console)
        await serve(agent, args.host, args.port, asyncio.Event(), tls)
    elif args.command == "client":
        token = os.environ.get("REMOTE_GPU_TOKEN")
        if not token:
            token = Path(args.token_file).read_text(encoding="utf-8").strip() if args.token_file else getpass.getpass("连接密钥：")
        def stats(data):
            rtt = f"{data['rtt_ms']:.0f}" if data["rtt_ms"] is not None else "--"
            console(f"回传 {data['return_fps']:.1f} FPS | 上行 {data['upload_mbps']:.2f} Mbps | "
                    f"下行 {data['download_mbps']:.2f} Mbps | 数据通道 RTT {rtt} ms | 写摄像头 {data['camera_writes']}")
        await connect_client(args.url, token, args.camera,
                             VideoSettings(args.width, args.height, args.fps, args.bitrate), asyncio.Event(),
                             args.sink, args.ice_config, status=console, statistics=stats)


def main():
    args = parser().parse_args()
    report_root = reports_directory()
    logging.basicConfig(level=logging.WARNING,
                        handlers=[RotatingFileHandler(report_root / "remote-gpu.log", maxBytes=2_000_000,
                                                      backupCount=2, encoding="utf-8")])
    if args.command == "preview-benchmark":
        from rgpu.preview_benchmark import run_preview_benchmark
        result = run_preview_benchmark(args.seconds, args.portrait, args.legacy)
        save_report(args.report, result)
        console(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("ok"):
            raise SystemExit(1)
        return
    if args.command in (None, "gui"):
        from rgpu.gui import Application, launch
        if getattr(args, "smoke_report", None):
            app = Application(args.role)
            app.withdraw()
            app.update()
            save_report(args.smoke_report, {"ok": True, "title": app.title(), "role": args.role,
                                            "tk_version": app.tk.call("info", "patchlevel")})
            app.destroy()
        else:
            default_role = "agent" if Path(sys.executable).stem.lower().endswith("server") else "local"
            launch(getattr(args, "role", default_role), getattr(args, "preview_camera", None), getattr(args, "portrait", False))
        return
    if args.command == "doctor":
        result = report(args.probe_nvenc)
        save_report(args.report, result)
        console(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "camera-test":
        from rgpu.camtest import virtual_camera_selftest
        result = virtual_camera_selftest(channel=args.channel)
        save_report(args.report, result.as_dict())
        console(result.text())
    elif args.command == "cameras":
        from rgpu.media import list_cameras
        for device in list_cameras():
            console(f"{device.name}\n  {device.id}")
    elif args.command == "windows":
        from rgpu.window_capture import list_windows
        for window in list_windows():
            console(f"HWND={window.hwnd} PID={window.pid} {window.label}")
    else:
        asyncio.run(async_main(args))


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        if sys.stderr:
            print(f"错误：{ex}", file=sys.stderr)
        else:
            try:
                save_report(reports_directory() / "last-error.json", {"error": str(ex)})
            except OSError:
                pass
        raise SystemExit(1)
