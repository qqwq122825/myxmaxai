using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using FaceLive.Services;
using Microsoft.Win32;
using Xmax.SDK;

namespace FaceLive;

public partial class MainWindow : Window
{
    private static readonly (string Label, int Width, int Height)[] Resolutions =
    [
        ("竖屏 720 × 1280", 720, 1280),
        ("竖屏 1080 × 1920", 1080, 1920),
        ("横屏 1280 × 720", 1280, 720),
        ("横屏 1920 × 1080", 1920, 1080),
    ];

    /// <summary>Camera capture target: portrait output uses a portrait camera canvas so nothing is wasted.</summary>
    private VideoFormat CameraFormat => vcam.OutputHeight > vcam.OutputWidth ? new(1080, 1920, 30) : new(1920, 1080, 30);

    private readonly AppSettings settings = AppSettings.Load();
    private readonly IReadOnlyList<ReferenceItem> presets = PresetCatalog.Load();
    private readonly ObservableCollection<ReferenceItem> visibleReferences = [];
    private readonly VirtualCamera vcam = new();
    private readonly DispatcherTimer uiTimer = new() { Interval = TimeSpan.FromMilliseconds(500) };
    private readonly Thread idleFeeder;
    private readonly byte[] idleFrame = new byte[16 * 16 * 4];

    private XmaxRealtime? session;
    private string sessionSignature = "";
    private int revision;
    private bool generating, busy, loading = true, closing, closed;
    private DateTime? generationStartedUtc;
    private long lastRemoteFrameTick;
    private string currentReferenceUrl = "";

    public MainWindow()
    {
        InitializeComponent();

        for (int i = 0; i < idleFrame.Length; i += 4) idleFrame[i + 3] = 255; // opaque black
        idleFeeder = new Thread(IdleFeedLoop) { IsBackground = true, Name = "VirtualCam idle feeder" };

        ApiKeyBox.Password = settings.ApiKey;
        EnvironmentBox.SelectedIndex = settings.GlobalEnvironment ? 0 : 1;
        PromptBox.Text = settings.CustomPrompt;
        FaceRestoreBox.IsChecked = settings.FaceRestoration;
        MirrorBox.IsChecked = settings.MirrorOutput;
        FlipBox.IsChecked = settings.FlipVertical;
        AutoStopBox.Text = settings.AutoStopMinutes.ToString();
        vcam.Mirror = settings.MirrorOutput;
        vcam.FlipVertical = settings.FlipVertical;
        currentReferenceUrl = settings.ReferenceUrl;

        foreach (var r in Resolutions) ResolutionBox.Items.Add(r.Label);
        int resIndex = Array.FindIndex(Resolutions, r => r.Width == settings.OutputWidth && r.Height == settings.OutputHeight);
        ResolutionBox.SelectedIndex = resIndex >= 0 ? resIndex : 0;
        vcam.SetOutputResolution(Resolutions[ResolutionBox.SelectedIndex].Width, Resolutions[ResolutionBox.SelectedIndex].Height);

        foreach (var (mode, name) in PresetCatalog.ModeNames)
        {
            var radio = new RadioButton
            {
                Content = name, Tag = mode, GroupName = "mode", Margin = new Thickness(0, 0, 18, 0),
                Foreground = (System.Windows.Media.Brush)FindResource("Text"), IsChecked = mode == settings.Mode,
            };
            radio.Checked += Mode_Checked;
            ModePanel.Children.Add(radio);
        }
        ReferenceList.ItemsSource = visibleReferences;
        RefreshReferenceList();

        uiTimer.Tick += (_, _) => UpdateLiveInfo();
        uiTimer.Start();
        Loaded += async (_, _) =>
        {
            loading = false;
            idleFeeder.Start();
            UpdateVcamStatus();
            await RefreshCamerasAsync();
        };
        Closing += Window_Closing;
        UpdateButtons();
    }

    // ───────────────────────── Camera & session ─────────────────────────

    private async Task RefreshCamerasAsync()
    {
        try
        {
            using var client = new XmaxClient();
            var cameras = await client.ListCamerasAsync();
            // Never offer our own virtual camera as an input — that would feed the output back in.
            var list = cameras.Where(c => !c.Name.Contains(VirtualCameraInstaller.DeviceName, StringComparison.OrdinalIgnoreCase)
                                       && !c.Name.Contains("Unity Video Capture", StringComparison.OrdinalIgnoreCase)).ToList();
            CameraBox.ItemsSource = list;
            CameraBox.SelectedItem = list.FirstOrDefault(c => c.Id == settings.CameraId) ?? list.FirstOrDefault();
            if (list.Count == 0) SetStatus("没有找到摄像头：请检查摄像头连接，以及 Windows 设置 → 隐私 → 相机 是否允许桌面应用访问。");
        }
        catch (Exception ex) { ShowError(ex); }
    }

    private string CurrentApiKey => ApiKeyBox.Password.Trim();
    private XmaxEnvironment CurrentEnvironment => EnvironmentBox.SelectedIndex == 1 ? XmaxEnvironment.China : XmaxEnvironment.Global;

    /// <summary>Creates (or reuses) the realtime session and starts the free local camera preview.</summary>
    private async Task EnsureSessionAsync()
    {
        if (CameraBox.SelectedItem is not CameraDevice camera) throw new InvalidOperationException("请先选择一个真实摄像头。");
        string signature = $"{CurrentApiKey}|{CurrentEnvironment}|{camera.Id}|{CameraFormat}";
        if (session is not null && signature == sessionSignature) return;

        await CloseSessionAsync();
        int current = ++revision;
        using var client = new XmaxClient(new XmaxConfiguration { ApiKey = CurrentApiKey, Environment = CurrentEnvironment });
        var next = client.CreateRealtime(new RealtimeConfiguration { Model = "x2.1" });
        session = next;
        sessionSignature = signature;
        next.EventReceived += e => Dispatcher.InvokeAsync(() => { if (current == revision) OnRealtimeEvent(e); });
        next.CallbackFaulted += ex => Dispatcher.InvokeAsync(() => { if (current == revision) ShowError(ex); });

        LocalView.VideoSource = next.LocalVideo;
        RemoteView.VideoSource = next.RemoteVideo;
        await next.SetVideoMirrorAsync(new VideoMirrorConfiguration(LocalPreview: true, RemoteView: false, PublishedVideo: false));
        await next.SetRemoteMediaListenerAsync(OnRemoteMedia);

        SetStatus("正在打开摄像头…");
        await next.PrepareCameraAsync(camera.Id, CameraFormat, microphone: false);
        if (current == revision) SetStatus("摄像头已就绪（预览不收费）");
    }

    private async Task CloseSessionAsync()
    {
        var previous = session;
        session = null;
        sessionSignature = "";
        ++revision;
        SetGenerating(false);
        LocalView.VideoSource = null;
        RemoteView.VideoSource = null;
        if (previous is null) return;
        try { await previous.SetRemoteMediaListenerAsync(null); } catch { }
        try { await previous.DisposeAsync(); } catch { }
    }

    /// <summary>Runs on an SDK background thread for every generated frame: forward it to the virtual camera.</summary>
    private void OnRemoteMedia(XmaxMediaFrame frame)
    {
        using (frame)
        {
            if (frame.Type != MediaType.Video) return;
            int width = (int)frame.VideoWidth, height = (int)frame.VideoHeight;
            try
            {
                frame.ReadBgra((pixels, stride, _) => vcam.Send(pixels, width, height, stride));
                Interlocked.Exchange(ref lastRemoteFrameTick, Environment.TickCount64);
            }
            catch (NotSupportedException) { }
        }
    }

    /// <summary>Keeps the virtual camera alive with a black frame whenever no generated frames are flowing,
    /// so 直播伴侣 shows black instead of the driver's test pattern.</summary>
    private void IdleFeedLoop()
    {
        var handle = GCHandle.Alloc(idleFrame, GCHandleType.Pinned);
        try
        {
            while (!closed)
            {
                if (Environment.TickCount64 - Interlocked.Read(ref lastRemoteFrameTick) > 500)
                    vcam.Send(handle.AddrOfPinnedObject(), 16, 16, 16 * 4);
                Thread.Sleep(100);
            }
        }
        finally { handle.Free(); }
    }

    // ───────────────────────── Start / stop ─────────────────────────

    private async void StartButton_Click(object sender, RoutedEventArgs e)
    {
        if (busy) return;
        if (generating) { await StopGenerationAsync("已停止"); return; }
        await RunBusyAsync(StartGenerationAsync);
    }

    private async Task StartGenerationAsync()
    {
        if (string.IsNullOrEmpty(CurrentApiKey)) throw new InvalidOperationException("请先填写 API Key。");
        if (string.IsNullOrEmpty(currentReferenceUrl)) throw new InvalidOperationException("请先选择一张参考图。");
        if (!VirtualCameraInstaller.IsInstalled())
        {
            SetStatus("第一次使用，正在安装虚拟摄像头（请在弹出的窗口点“是”）…");
            if (!await VirtualCameraInstaller.InstallAsync())
                throw new InvalidOperationException("虚拟摄像头安装未完成：需要在弹出的管理员授权窗口点“是”（安全软件拦截时请允许）。");
            UpdateVcamStatus();
        }

        await EnsureSessionAsync();
        var current = session!;
        SetStatus("正在连接服务器，等待第一帧（从这里开始计费）…");
        await current.StartAsync(BuildContext(), microphone: false);
        if (current != session) return;
        SetGenerating(true);
        SetStatus($"虚拟摄像头已启动（{vcam.OutputWidth}×{vcam.OutputHeight}）· 在直播伴侣里添加摄像头「{VirtualCameraInstaller.DeviceName}」");
    }

    private async Task StopGenerationAsync(string reason)
    {
        var current = session;
        if (current is null) { SetGenerating(false); return; }
        busy = true; UpdateButtons();
        try
        {
            await current.DisconnectAsync();   // stops billing, keeps the local preview
            SetStatus(reason + " · 计费已停止");
        }
        catch (Exception ex)
        {
            ShowError(ex);
            await CloseSessionAsync();           // make sure the connection is really gone
        }
        finally { SetGenerating(false); busy = false; UpdateButtons(); }
    }

    private GenerationContext BuildContext()
    {
        string prompt = string.IsNullOrWhiteSpace(PromptBox.Text) ? PresetCatalog.DefaultPrompts[CurrentMode] : PromptBox.Text.Trim();
        return new GenerationContext(prompt, currentReferenceUrl, FaceRestoreBox.IsChecked == true);
    }

    private void SetGenerating(bool value)
    {
        generating = value;
        generationStartedUtc = value ? DateTime.UtcNow : null;
        RemotePlaceholder.Visibility = value ? Visibility.Collapsed : Visibility.Visible;
        LiveBadge.Visibility = value ? Visibility.Visible : Visibility.Collapsed;
        UpdateButtons();
    }

    private void OnRealtimeEvent(RealtimeEvent e)
    {
        switch (e.Type)
        {
            case RealtimeEventType.State:
                if (generating && e.State is RealtimeState.Ready or RealtimeState.Idle)
                {
                    SetGenerating(false);
                    SetStatus("连接已断开 · 计费已停止");
                }
                break;
            case RealtimeEventType.Error:
                ErrorText.Text = DescribeError(e.Message, e.Code, e.ApiCode, e.HttpStatus);
                break;
            case RealtimeEventType.Statistics when e.Direction == 1 && e.Width > 0:
                MetricsText.Text = $"输出 {e.Width}×{e.Height} · {Num(e.FramesPerSecond)} fps · {Num(e.BitrateKbps)} kbps · 延迟 {Num(e.EndToEndMilliseconds)} ms";
                break;
            case RealtimeEventType.Timing when e.Code >= 0 && e.FirstFrameMilliseconds > 0:
                MetricsText.Text = $"首帧耗时 {e.FirstFrameMilliseconds:F0} ms";
                break;
        }
    }

    private static string Num(double v) => v >= 0 ? v.ToString("F0") : "—";

    private void UpdateLiveInfo()
    {
        if (generationStartedUtc is { } start)
        {
            var elapsed = DateTime.UtcNow - start;
            ElapsedText.Text = elapsed.ToString(@"hh\:mm\:ss");
            CreditText.Text = $"本次约消耗 {(long)elapsed.TotalSeconds} 积分（1 积分/秒）";
            if (int.TryParse(AutoStopBox.Text, out int limit) && limit > 0 && elapsed.TotalMinutes >= limit && !busy)
                _ = StopGenerationAsync($"已到达自动停止时间（{limit} 分钟）");
        }
        UpdateVcamLive();
    }

    // ───────────────────────── References ─────────────────────────

    private string CurrentMode => ModePanel.Children.OfType<RadioButton>().FirstOrDefault(r => r.IsChecked == true)?.Tag as string ?? "charx";

    private void Mode_Checked(object sender, RoutedEventArgs e)
    {
        if (loading) return;
        settings.Mode = CurrentMode;
        settings.Save();
        RefreshReferenceList();
    }

    private void RefreshReferenceList()
    {
        string mode = CurrentMode;
        visibleReferences.Clear();
        foreach (var u in settings.UserReferences.Where(u => u.Mode == mode))
            visibleReferences.Add(new ReferenceItem(Path.GetFileNameWithoutExtension(u.Name), u.Mode, File.Exists(u.Name) ? u.Name : null, u.Url, IsUserImage: true));
        foreach (var p in presets.Where(p => p.Mode == mode)) visibleReferences.Add(p);
        var selected = visibleReferences.FirstOrDefault(r => r.Url == currentReferenceUrl);
        loading = true;
        ReferenceList.SelectedItem = selected;
        loading = false;
    }

    private async void ReferenceList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (loading || ReferenceList.SelectedItem is not ReferenceItem item) return;
        currentReferenceUrl = item.Url;
        settings.ReferenceUrl = item.Url;
        settings.ReferenceName = item.Name;
        settings.Save();
        if (generating && session is { } current)
        {
            try
            {
                await current.UpdateAsync(BuildContext());
                SetStatus($"已切换为「{DisplayName(item)}」");
            }
            catch (Exception ex) { ShowError(ex); }
        }
    }

    private static string DisplayName(ReferenceItem item) => item.Name;

    private async void UploadImage_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrEmpty(CurrentApiKey)) { ShowError(new InvalidOperationException("上传图片需要先填写 API Key。")); return; }
        var dialog = new OpenFileDialog { Filter = "图片|*.png;*.jpg;*.jpeg;*.webp", Title = "选择参考图（清晰、背景干净）" };
        if (dialog.ShowDialog(this) != true) return;
        string path = dialog.FileName;
        if (new FileInfo(path).Length > 20 * 1024 * 1024) { ShowError(new InvalidOperationException("图片不能超过 20 MB。")); return; }

        UploadButton.IsEnabled = false;
        SetStatus("正在上传参考图…");
        try
        {
            using var client = new XmaxClient(new XmaxConfiguration { ApiKey = CurrentApiKey, Environment = CurrentEnvironment });
            await using var storage = client.CreateStorage();
            var uploaded = await storage.UploadFileAsync(path, new UploadOptions(CheckImageSafety: true));
            settings.UserReferences.Insert(0, new UserReference(path, uploaded.Url, CurrentMode));
            currentReferenceUrl = uploaded.Url;
            settings.ReferenceUrl = uploaded.Url;
            settings.Save();
            RefreshReferenceList();
            ReferenceList.SelectedIndex = 0;
            SetStatus("参考图已上传");
        }
        catch (Exception ex) { ShowError(ex); }
        finally { UploadButton.IsEnabled = true; }
    }

    // ───────────────────────── Virtual camera ─────────────────────────

    private void UpdateVcamStatus()
    {
        bool installed = VirtualCameraInstaller.IsInstalled();
        VcamButton.Content = installed ? "卸载" : "一键安装";
        VcamStatusText.Text = installed ? $"已安装「{VirtualCameraInstaller.DeviceName}」" : "未安装（需要管理员权限，只装一次）";
        VcamStatusText.Foreground = (System.Windows.Media.Brush)FindResource(installed ? "Ok" : "Warn");
        UpdateVcamLive();
    }

    private void UpdateVcamLive()
    {
        if (!VirtualCameraInstaller.IsInstalled()) return;
        bool watched = vcam.IsBeingWatched;
        VcamStatusText.Text = $"已安装「{VirtualCameraInstaller.DeviceName}」" + (watched ? " · 直播软件已接收画面" : " · 等待直播软件打开");
    }

    private async void VcamButton_Click(object sender, RoutedEventArgs e)
    {
        VcamButton.IsEnabled = false;
        try
        {
            if (VirtualCameraInstaller.IsInstalled())
            {
                if (MessageBox.Show(this, "卸载后直播软件里将看不到这个摄像头，确定卸载吗？", "卸载虚拟摄像头",
                        MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;
                vcam.Reset();
                bool ok = await VirtualCameraInstaller.UninstallAsync();
                SetStatus(ok ? "虚拟摄像头已卸载" : "卸载未完成（可能取消了管理员授权）");
            }
            else
            {
                bool ok = await VirtualCameraInstaller.InstallAsync();
                SetStatus(ok
                    ? $"虚拟摄像头已安装。如果直播伴侣已经打开，请重启它，然后在摄像头里选择「{VirtualCameraInstaller.DeviceName}」。"
                    : "安装未完成（可能取消了管理员授权，或被安全软件拦截）");
            }
        }
        finally { VcamButton.IsEnabled = true; UpdateVcamStatus(); }
    }

    private void OutputOptions_Changed(object sender, RoutedEventArgs e)
    {
        vcam.Mirror = MirrorBox.IsChecked == true;
        vcam.FlipVertical = FlipBox.IsChecked == true;
        Settings_Changed(sender, e);
    }

    // ───────────────────────── Settings & misc ─────────────────────────

    private void Settings_Changed(object sender, RoutedEventArgs e)
    {
        if (loading) return;
        settings.CustomPrompt = PromptBox.Text.Trim();
        settings.FaceRestoration = FaceRestoreBox.IsChecked == true;
        settings.MirrorOutput = MirrorBox.IsChecked == true;
        settings.FlipVertical = FlipBox.IsChecked == true;
        settings.AutoStopMinutes = int.TryParse(AutoStopBox.Text, out int m) && m > 0 ? m : 0;
        settings.Save();
    }

    private void ApiKeyBox_PasswordChanged(object sender, RoutedEventArgs e)
    {
        if (loading) return;
        settings.ApiKey = CurrentApiKey;
        settings.Save();
    }

    private void EnvironmentBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (loading) return;
        settings.GlobalEnvironment = EnvironmentBox.SelectedIndex != 1;
        settings.Save();
    }

    private async void CameraBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (CameraBox.SelectedItem is not CameraDevice camera) return;
        settings.CameraId = camera.Id;
        settings.Save();
        if (generating) { SetStatus("换摄像头前请先停止生成"); return; }
        await RunBusyAsync(EnsureSessionAsync);
    }

    private async void ResolutionBox_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (loading || ResolutionBox.SelectedIndex < 0) return;
        var r = Resolutions[ResolutionBox.SelectedIndex];
        vcam.SetOutputResolution(r.Width, r.Height);
        settings.OutputWidth = r.Width;
        settings.OutputHeight = r.Height;
        settings.Save();
        SetStatus($"输出分辨率已改为 {r.Width}×{r.Height}。如果直播伴侣里已经添加了这个摄像头，请删掉重新添加一次。");
        if (CameraBox.SelectedItem is CameraDevice && !generating) await RunBusyAsync(EnsureSessionAsync);
    }

    private async void RefreshCameras_Click(object sender, RoutedEventArgs e) => await RefreshCamerasAsync();

    private async Task RunBusyAsync(Func<Task> action)
    {
        busy = true; UpdateButtons();
        ErrorText.Text = "";
        try { await action(); }
        catch (Exception ex) { ShowError(ex); }
        finally { busy = false; UpdateButtons(); }
    }

    private void UpdateButtons()
    {
        StartButton.IsEnabled = !busy;
        StartButton.Content = busy ? "请稍候…" : generating ? "停止虚拟摄像头（停止计费）" : "启动虚拟摄像头";
        CameraBox.IsEnabled = ResolutionBox.IsEnabled = !generating && !busy;
        ApiKeyBox.IsEnabled = EnvironmentBox.IsEnabled = !generating;
    }

    private void SetStatus(string text) => StatusText.Text = text;

    private void ShowError(Exception ex)
    {
        ErrorText.Text = ex is XmaxException x ? DescribeError(x.Message, x.Result, x.ApiCode, x.HttpStatus) : ex.Message;
    }

    private static string DescribeError(string message, int code, int apiCode, int http)
    {
        string hint = apiCode switch
        {
            46002 => "API Key 无效，请检查，或确认国际版/国内版选对了。",
            46018 => "临时 Key 已过期。",
            46019 => "Key 的积分额度已用完。",
            46032 => "这个临时 Key 的同时在线数已满。",
            _ => code switch
            {
                -14 => "API Key 或服务环境（国际版/国内版）不对。",
                -16 or -17 => "没有摄像头权限：Windows 设置 → 隐私 → 相机，打开“允许桌面应用访问相机”。",
                -3 or -4 or -5 or -6 or -15 => "网络连接出问题了，请检查网络/代理后重试。",
                -11 or -12 or -13 or -18 or -19 => "参考图有问题（可能没通过安全审核），请换一张。",
                _ => "",
            },
        };
        string detail = $"{message}（SDK {code}, API {apiCode}, HTTP {http}）";
        return string.IsNullOrEmpty(hint) ? detail : hint + "  " + detail;
    }

    private async void Window_Closing(object? sender, CancelEventArgs e)
    {
        if (closing) return;
        e.Cancel = true;                 // finish closing the session first so billing really stops
        closing = true;
        uiTimer.Stop();
        try { await CloseSessionAsync(); } catch { }
        closed = true;
        vcam.Dispose();
        Close();
    }
}
