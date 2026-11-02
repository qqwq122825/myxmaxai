# DeepFaceLive 结果虚拟摄像头补丁 1.0.0

适用于本次 `DeepFaceLive_Official/DeepFaceLive_NVIDIA` Python 3.8 整合包。基于 iperov/DeepFaceLive（GPL-3.0）修改，保留许可证及修改源码。共享内存使用已有 FaceLive/UnityCapture 协议，驱动及原协议的 MIT 许可随包提供。

## 使用

1. 在 GPU 服务器**关闭 DeepFaceLive、桥接端、直播伴侣和摄像头检查器**。
2. 将 ZIP 内 `DeepFaceLive_NVIDIA` 文件夹合并到原同名目录。不要移动模型、userdata 或 Python 运行库。
3. 在包含 `DeepFaceLive.bat` 的目录双击 **apply-result-camera.cmd**。它校验原文件版本、备份后应用源码补丁；若提示 local changes/version mismatch，不会覆盖未知修改。
4. 右键同目录 **install-result-camera.cmd → 以管理员身份运行**。它注册三个独立设备，并保存路径/哈希/退出码报告。以后在这台服务器更新驱动时仍用这个三设备脚本，普通两设备安装脚本会取消第三路注册。
5. 启动桥接端，提供输入虚拟摄像头 **FaceLive Camera #2**；启动手机推流。
6. 启动 DeepFaceLive。摄像头源选 **FaceLive Camera #2**；「视频输出」来源选「合成后的画面」，点击新增 **启动虚拟摄像头**。
7. 状态会显示 **Camera #3 · 实际宽×高 · 等待读取端/正在输出**。输出是当前选中的结果图像，窗口显示可以关闭。
8. 桥接服务器「结果来源」选 **结果虚拟摄像头** → 刷新设备 → 选 **FaceLive Camera #3** → 预览结果 → 开始回传。本地接收方式不变。

不要在 DeepFaceLive 的输入选择 Camera #3，也不要在桥接端把结果选成输入 Camera #2，否则会形成反馈或回传原画。

## 按钮与行为

- 「启动虚拟摄像头」「关闭虚拟摄像头」只控制结果 Camera #3，不停止换脸推理或桥接输入。
- 不默认自动启动，不保存自动开启状态；缺少第三路驱动时显示安装提示，按钮可重试。
- 当前结果帧最大支持 1920×1080 / 1080×1920；不会把实际 720p 冒充为 1080p。对比拼接画面若超过限制会提示改用单独合成画面。
- Camera #3 的状态显示 DeepFaceLive 内部实际输出尺寸。桥接端仍可能按会话设置缩放；将 720p 的源放大到 1080p 不会增加原始细节。
- 有独立的最新帧写入线程，丢弃待处理的旧结果，不积压输出队列。摄像头按 30 FPS 输出不等于模型生成了 30 个不同的结果帧。
- 一秒没有新结果时，Camera #3 输出黑画面；独立结果窗口也会清黑，避免继续显示上一次的人脸。
- 此前截图显示「推流设备已断开」，输入因此变黑；独立窗口保留上一帧是旧输出逻辑未清理导致。这里没有把“无脸时跳过推理”的高帧率当成真实换脸性能。
- 直接结果摄像头绕过桌面窗口采集和窗口标题/边框，但仍有图像转换、共享内存、DirectShow 读取与网络编码开销，不承诺零拷贝或一定达到 30 FPS。

## 文件与回退

补丁只改 `StreamOutput.py`、`QStreamOutput.py`，新增 `FaceLiveVirtualCamera.py`、`FaceLiveUnity.py`；没有修改 CameraSource、现有 YY 兼容适配、人脸检测/融合/模型算法和模型文件。

应用时原文件备份在 `DeepFaceLive_NVIDIA/FaceLivePatchBackups/时间/`。需要回退时关闭 DeepFaceLive，将备份中的 `apps/DeepFaceLive/backend/StreamOutput.py` 和 `apps/DeepFaceLive/ui/QStreamOutput.py` 按相对路径复制回 `_internal/DeepFaceLive/`，即可恢复原输出逻辑。第三路驱动注册可保留，不影响前两路。

## 验证范围

已用整合包自带 Python 3.8.10 / NumPy 1.21.6 / OpenCV 4.8.0 测试：1080×1920 真实独立共享内存写入、RGBA 顺序与上下方向、浮点转换不修改输入、启停/重启/尺寸变化、超时黑屏、只开虚拟摄像头而不开窗口的输出路径、驱动缺失提示、Qt 两个按钮、旧结果窗口清黑。没有启动本机真实 Camera #1/#2 写入，也没有注册或替换本机系统驱动。

服务器 Camera #3 的真实注册、DeepFaceLive 模型运行后的结果与桥接回传，仍需在目标机上安装后核对。可先保留 Windows 图形采集作为已验证的备用路径。

上游：[iperov/DeepFaceLive](https://github.com/iperov/DeepFaceLive)、[GPL-3.0](https://github.com/iperov/DeepFaceLive/blob/master/LICENSE)。
