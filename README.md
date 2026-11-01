# FaceLive · 换脸直播助手

基于 [Xmax X2.1](https://platform.xmax.ai/docs) 实时视频生成的 Windows 桌面程序：
真实摄像头 → 云端实时换人物 / 换衣服 / 换风格 → 输出到虚拟摄像头「FaceLive Camera」→ 直播伴侣 / OBS 直接添加。

## 下载

- 每次推送到 `main`：Actions → 最新一次 “Build Windows exe” → 页面底部 Artifacts 下载 `FaceLive-win-x64`
- 打 tag（如 `v0.2.0`）：自动生成 Release，附带 `FaceLive-win-x64.zip`

使用方法见 [docs/使用说明.txt](docs/使用说明.txt)。

## 目录

| 路径 | 内容 |
|---|---|
| `app/` | C# / WPF / .NET 10 主程序 |
| `app/Services/VirtualCamera.cs` | 生成画面缩放裁切到输出分辨率，写入虚拟摄像头共享内存 |
| `app/Services/VirtualCameraInstaller.cs` | 一键注册 / 卸载虚拟摄像头驱动（UAC 授权一次） |
| `vcam/` | 虚拟摄像头 DirectShow 驱动，改自 [UnityCapture](https://github.com/schellingb/UnityCapture)（MIT）：增加竖屏分辨率，并从 `HKCU\Software\FaceLive\VirtualCam` 读取程序里选的分辨率 |
| `.github/workflows/build.yml` | 云端编译驱动 + 程序并打包 |

## 本地编译

```
# 1. 从 https://github.com/Xmax-AI/XmaxSDK-Windows/releases/tag/1.1.0
#    下载 Xmax.SDK.1.1.0.nupkg、Xmax.SDK.Wpf.1.1.0.nupkg 放进 packages/
# 2. 驱动（需要 VS2022 C++）
msbuild vcam/UnityCaptureFilter.vcxproj /p:Configuration=Release /p:Platform=x64 /p:PlatformToolset=v143
# 3. 程序
dotnet publish app -c Release -o out/FaceLive
```

## 许可

- Xmax SDK：MIT（Xmax-AI/XmaxSDK-Windows）
- 虚拟摄像头驱动：MIT（Bernhard Schelling / UnityCapture）
