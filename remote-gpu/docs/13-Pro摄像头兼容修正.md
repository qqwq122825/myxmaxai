# Pro 摄像头兼容修正（0.4.2）

用户在 Windows Server 2022（20348）上的 DeepLiveCam 3 Pro 3.2.0 报错：
`DLC_PLATFORM_DEVICE_UNAVAILABLE | dshow_open_failed`。
此错误发生在输入摄像头打开阶段，尚不涉及结果窗口采集。

## 已修改的桥接端代码

1. 每路 FaceLive 摄像头注册独立的 DevicePath，替代旧版共用的 `foo:bar`，避免软件按设备路径匹配到另一通道。
2. Camera #2 优先公布发送端尺寸，同时提供 540p、720p、1080p 和竖屏等常用模式；不再只公布一种尺寸。
3. DirectShow 根据像素格式、尺寸、缓冲区大小和帧间隔验证媒体类型，不再要求整个描述结构逐字节相同。允许 RGB24、RGB32、ARGB32，允许 25 FPS；拒绝不支持的负高度、带行填充的 RGB24 和畸形尺寸。
4. 协商完成后同步实际用于分配和填充画面的媒体类型。限制已连接时改变缓冲区尺寸，避免越界。
5. 修正高分辨率能力描述中的码率整数溢出，避免向软件公布负数最大码率。
6. Camera #2 的首选尺寸独立存入 `HKCU\Software\FaceLive\VirtualCam\Channels\1`，本地输出摄像头继续使用原设置。
7. 服务器启动自检时比较安装的 DLL 和包内 DLL，防止只更新 EXE、仍加载旧驱动却当作已完成兼容更新。

Pro 安装目录中未找到完整源码；本次未修改 Pro EXE、授权或模型。

## 服务器更新步骤

1. 先停止 Pro 和服务器桥接程序，并关闭使用 FaceLive 摄像头的检查器。
2. 将更新包中的 RemoteGpu 文件夹合并覆盖原目录，保留 `_internal` 中其余文件。
3. 在 RemoteGpu 目录打开**管理员 PowerShell**，执行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\deploy\install-driver.ps1
   ```

   **这一步不能省略。** 驱动实际从 ProgramData 加载，覆盖发布包里的 DLL 不会自动更新已安装驱动。
   安装脚本会在 `%ProgramData%\FaceLive\VirtualCam\Backups` 保存被替换的旧 DLL。

4. 重新打开服务器端，点击「启动输入虚拟摄像头并自检」。
5. 本地端或手机保持推送，确认服务器原画持续变化。
6. 重启 Pro、刷新摄像头列表，明确选择 **FaceLive Camera #2**，不是 **FaceLive Camera**。先以 720p / 普通画质测试。
7. Pro 读取成功后启动换脸、打开「单独换脸窗口」，在桥接端刷新窗口、预览对应结果窗口，再开始回传。

Pro 自带的 DLC-Pro 输出虚拟摄像头安装脚本要求 Windows 构建号至少 22000，当前 Server 2022 不满足。结果继续使用独立窗口采集。

## 验证边界

- 已使用旧 DLL 复现能力协商测试失败；新 DLL 的 64 位、32 位原生测试均检查多尺寸、25 FPS、RGB24/RGB32、可选描述字段和无效媒体类型拒绝。
- 原生测试直接创建 DLL 内的测试通道，不注册驱动，不打开用户的物理摄像头。
- 共享内存色彩和方向测试、桥接自动化测试另行执行。
- 这些测试验证驱动修正；**仍需在服务器上用 Pro 实际读取并验证换脸画面，不能把原生测试通过当作 Pro 联调完成。**

## 重新编译驱动

使用 VS 2022 C++ Build Tools 和 Windows SDK，在项目根目录运行 `vcam\build.ps1`。
可通过 `-MSBuild` 和 `-WindowsSdk` 指定本机路径及版本。输出在 `vcam\Build`，脚本不安装驱动。
原生检查程序为 `vcam\compatibility_test.vcxproj`，传入对应位数的 DLL 完整路径运行。
