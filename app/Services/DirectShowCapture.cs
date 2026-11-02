using System.Diagnostics;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using Xmax.SDK;

namespace FaceLive.Services;

/// <summary>Reads DirectShow devices in a separate FFmpeg process and submits local frames to the SDK.</summary>
public sealed class DirectShowCapture : IAsyncDisposable
{
    private static string Executable => Path.Combine(AppContext.BaseDirectory, "Tools", "ffmpeg.exe");
    public static bool IsAvailable => File.Exists(Executable);
    private readonly ExternalFrameSink sink;
    private readonly CancellationTokenSource stop = new();
    private readonly TaskCompletionSource firstFrame = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly Queue<string> errors = new();
    private Process? process;
    private Task? reader, errorReader;
    private bool disposed;
    public event Action<Exception>? Faulted;
    public long FramesReceived { get; private set; }
    public int InputWidth { get; private set; }
    public int InputHeight { get; private set; }

    public DirectShowCapture(ExternalFrameSink sink) => this.sink = sink;

    private static ProcessStartInfo CreateStartInfo(params string[] arguments)
    {
        if (!File.Exists(Executable))
            throw new FileNotFoundException("缺少 Tools/ffmpeg.exe，请解压完整程序包，或切换到 Media Foundation 后端。", Executable);
        var info = new ProcessStartInfo(Executable)
        {
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var argument in arguments) info.ArgumentList.Add(argument);
        return info;
    }

    public static async Task<IReadOnlyList<CameraDevice>> ListCamerasAsync()
    {
        using var probe = new Process { StartInfo = CreateStartInfo("-hide_banner", "-nostdin", "-list_devices", "true", "-f", "dshow", "-i", "dummy") };
        probe.Start();
        var output = probe.StandardOutput.ReadToEndAsync();
        var error = probe.StandardError.ReadToEndAsync();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15));
        try { await probe.WaitForExitAsync(timeout.Token); }
        catch (OperationCanceledException)
        {
            if (!probe.HasExited) probe.Kill(entireProcessTree: true);
            await probe.WaitForExitAsync();
            throw new TimeoutException("DirectShow 设备枚举超时，请关闭占用摄像头的软件后重试。");
        }
        await output;
        // FFmpeg intentionally exits with a nonzero code after -list_devices.
        return ParseDevices(await error);
    }

    internal static IReadOnlyList<CameraDevice> ParseDevices(string text)
    {
        var devices = new List<CameraDevice>();
        bool videoSection = false;
        foreach (string line in text.Split('\n'))
        {
            if (line.Contains("DirectShow video devices")) { videoSection = true; continue; }
            if (line.Contains("DirectShow audio devices")) { videoSection = false; continue; }
            var alternative = Regex.Match(line, "Alternative name \"([^\"]+)\"");
            if (alternative.Success)
            {
                if (videoSection && devices.Count > 0) devices[^1] = devices[^1] with { Id = alternative.Groups[1].Value };
                continue;
            }
            var device = Regex.Match(line, "\\]\\s+\"([^\"]+)\"\\s*(?:\\((video|audio)\\))?\\s*$");
            if (!device.Success) continue;
            if (device.Groups[2].Success) videoSection = device.Groups[2].Value == "video";
            if (videoSection) devices.Add(new(device.Groups[1].Value, device.Groups[1].Value, false));
        }
        return devices;
    }

    public async Task StartAsync(string deviceId, bool requestDeviceResolution = false, bool fillFrame = true)
    {
        if (process is not null) throw new InvalidOperationException("采集已经启动。");
        int width = (int)sink.Format.Width, height = (int)sink.Format.Height;
        // YY follows the resolution requested by its consumer. Leave the pixel format
        // negotiable: forcing I420 can fail even when the device advertises that mode.
        string filter = fillFrame
            ? $"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1"
            : $"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1";
        var info = CreateStartInfo("-hide_banner", "-loglevel", "info", "-nostats", "-nostdin", "-f", "dshow", "-rtbufsize", "64M");
        if (requestDeviceResolution)
        {
            info.ArgumentList.Add("-video_size");
            info.ArgumentList.Add($"{width}x{height}");
        }
        foreach (string argument in new[] { "-i", $"video={deviceId}", "-an", "-vf", filter, "-r",
            sink.Format.FramesPerSecond.ToString(System.Globalization.CultureInfo.InvariantCulture), "-pix_fmt", "bgra", "-f", "rawvideo", "pipe:1" })
            info.ArgumentList.Add(argument);
        process = new Process { StartInfo = info };
        process.Start();
        errorReader = ReadErrorsAsync();
        reader = Task.Run(ReadFramesAsync);
        try { await firstFrame.Task.WaitAsync(TimeSpan.FromSeconds(15)); }
        catch
        {
            await DisposeAsync();
            throw;
        }
    }

    private async Task ReadErrorsAsync()
    {
        while (await process!.StandardError.ReadLineAsync() is { } line)
        {
            if (InputWidth == 0)
            {
                var dimensions = Regex.Match(line, @"Video:.*?\b(\d{2,5})x(\d{2,5})\b");
                if (dimensions.Success)
                {
                    InputWidth = int.Parse(dimensions.Groups[1].Value);
                    InputHeight = int.Parse(dimensions.Groups[2].Value);
                }
            }
            lock (errors)
            {
                errors.Enqueue(line);
                if (errors.Count > 8) errors.Dequeue();
            }
        }
    }

    private async Task ReadFramesAsync()
    {
        int stride = checked((int)sink.Format.Width * 4);
        byte[] pixels = new byte[checked(stride * (int)sink.Format.Height)];
        var clock = Stopwatch.StartNew();
        using var frameTimeout = CancellationTokenSource.CreateLinkedTokenSource(stop.Token);
        long lastTimestamp = -1;
        try
        {
            while (!stop.IsCancellationRequested)
            {
                frameTimeout.CancelAfter(TimeSpan.FromSeconds(10));
                await process!.StandardOutput.BaseStream.ReadExactlyAsync(pixels, frameTimeout.Token);
                long timestamp = Math.Max(lastTimestamp + 1, clock.Elapsed.Ticks / 10);
                sink.PushBgra(pixels, stride, timestamp);
                lastTimestamp = timestamp;
                FramesReceived++;
                firstFrame.TrySetResult();
            }
        }
        catch (Exception ex) when (!stop.IsCancellationRequested)
        {
            try { if (!process!.HasExited) process.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
            if (errorReader is not null && process!.HasExited)
                try { await errorReader; } catch (IOException) { }
            string detail;
            lock (errors) detail = string.Join("\n", errors);
            var error = new IOException("DirectShow 无法继续读取画面。请确认 YY 开播已打开，且设备没有被独占。\n" + detail, ex);
            firstFrame.TrySetException(error);
            // A lost source must also tear down any billed cloud session.
            try { sink.ReportError(-11, error.Message); } catch { }
            Faulted?.Invoke(error);
        }
        catch (Exception) when (stop.IsCancellationRequested) { }
    }

    public async ValueTask DisposeAsync()
    {
        if (disposed) return;
        disposed = true;
        stop.Cancel();
        try
        {
            if (process is { } current)
            {
                try { if (!current.HasExited) current.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
                try { await current.WaitForExitAsync(); } catch (InvalidOperationException) { }
            }
            if (reader is not null) await reader;
            if (errorReader is not null) await errorReader;
        }
        finally
        {
            sink.Dispose();
            process?.Dispose();
            stop.Dispose();
        }
    }
}
