using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace FaceLive.Services;

/// <summary>
/// Sends frames to the FaceLive virtual camera (a patched "Unity Capture" DirectShow filter) through its
/// shared-memory protocol. Frames are scaled and center-cropped to the output resolution the streamer picked,
/// so 直播伴侣 receives exactly that size without stretching.
/// Protocol reference: vcam/shared.inl (MIT, Bernhard Schelling).
/// </summary>
public sealed unsafe class VirtualCamera : IDisposable
{
    private const string ResolutionKey = @"Software\FaceLive\VirtualCam";
    private const int HeaderBytes = 32; // maxSize, width, height, stride, format, resizemode, mirrormode, timeout
    private const uint SYNCHRONIZE = 0x00100000, EVENT_MODIFY_STATE = 0x0002, FILE_MAP_WRITE = 0x0002;
    private const uint WAIT_OBJECT_0 = 0, WAIT_ABANDONED = 0x80;

    private readonly object gate = new();
    private nint mutex, wantEvent, sentEvent, mapping;
    private byte* view;
    private long nextConnectAttempt, lastPullTick;
    private int outWidth = 720, outHeight = 1280;

    // Cached bilinear sampling tables (rebuilt when source or output size changes).
    private int mapSrcW, mapSrcH, mapDstW, mapDstH;
    private int[] xs0 = [], xs1 = [], xw = [], ys0 = [], ys1 = [], yw = [];

    public bool Mirror { get; set; }
    public bool FillFrame { get; set; } = true;

    /// <summary>DirectShow RGB bitmaps are bottom-up, so rows are written in reverse to appear upright.</summary>
    public bool FlipVertical { get; set; } = true;

    public int TimeoutMilliseconds { get; set; } = 3000;

    public long FramesSent { get; private set; }

    /// <summary>True when streaming software has asked for a frame within the last two seconds.</summary>
    public bool IsBeingWatched => Environment.TickCount64 - Interlocked.Read(ref lastPullTick) < 2000;

    public int OutputWidth => outWidth;
    public int OutputHeight => outHeight;

    /// <summary>
    /// Sets the resolution the virtual camera offers. Streaming software reads it when it opens the camera,
    /// so a change takes effect after the camera is re-added (or the streaming software restarts).
    /// </summary>
    public void SetOutputResolution(int width, int height)
    {
        lock (gate)
        {
            outWidth = width & ~3;
            outHeight = height & ~1;
        }
        try
        {
            using var key = Registry.CurrentUser.CreateSubKey(ResolutionKey);
            key.SetValue("Width", outWidth, RegistryValueKind.DWord);
            key.SetValue("Height", outHeight, RegistryValueKind.DWord);
        }
        catch { }
    }

    /// <summary>Scales one BGRA frame to the output resolution and hands it to the virtual camera.</summary>
    public bool Send(nint bgra, int width, int height, int strideBytes)
    {
        if (width <= 0 || height <= 0) return false;
        lock (gate)
        {
            if (!EnsureOpen()) return false;
            var header = (int*)view;
            int dw = outWidth, dh = outHeight;
            if ((uint)header[0] < (uint)(dw * dh * 4)) return false;

            uint wait = WaitForSingleObject(mutex, 200);
            if (wait != WAIT_OBJECT_0 && wait != WAIT_ABANDONED) return false;
            try
            {
                header[1] = dw;
                header[2] = dh;
                header[3] = dw;              // stride in pixels
                header[4] = 0;               // FORMAT_UINT8 (RGBA)
                header[5] = 1;               // RESIZEMODE_LINEAR as a safety net
                header[6] = Mirror ? 1 : 0;
                header[7] = TimeoutMilliseconds;
                var dst = (uint*)(view + HeaderBytes);
                if (width == dw && height == dh) CopyDirect((byte*)bgra, strideBytes, dst, dw, dh);
                else CopyScaled((byte*)bgra, strideBytes, width, height, dst, dw, dh);
            }
            finally { ReleaseMutex(mutex); }
            SetEvent(sentEvent);
            if (WaitForSingleObject(wantEvent, 0) == WAIT_OBJECT_0) Interlocked.Exchange(ref lastPullTick, Environment.TickCount64);
            FramesSent++;
            return true;
        }
    }

    private static uint Swap(uint p) => (p & 0xFF00FF00u) | ((p >> 16) & 0xFFu) | ((p & 0xFFu) << 16);

    private void CopyDirect(byte* src, int srcStride, uint* dst, int w, int h)
    {
        bool flip = FlipVertical;
        Parallel.For(0, h, Options, y =>
        {
            var s = (uint*)(src + (long)y * srcStride);
            var d = dst + (long)(flip ? h - 1 - y : y) * w;
            for (int x = 0; x < w; x++) d[x] = Swap(s[x]);
        });
    }

    /// <summary>Bilinear scale with center crop or letterboxing, BGRA → RGBA, optional vertical flip.</summary>
    private void CopyScaled(byte* src, int srcStride, int sw, int sh, uint* dst, int dw, int dh)
    {
        int contentW = dw, contentH = dh;
        if (!FillFrame)
        {
            double fit = Math.Min((double)dw / sw, (double)dh / sh);
            contentW = Math.Clamp((int)Math.Round(sw * fit), 1, dw);
            contentH = Math.Clamp((int)Math.Round(sh * fit), 1, dh);
        }
        int left = (dw - contentW) / 2, topOffset = (dh - contentH) / 2;
        BuildMaps(sw, sh, contentW, contentH);
        bool flip = FlipVertical;
        int[] X0 = xs0, X1 = xs1, XW = xw, Y0 = ys0, Y1 = ys1, YW = yw;
        Parallel.For(0, dh, Options, y =>
        {
            var d = dst + (long)(flip ? dh - 1 - y : y) * dw;
            if (contentW != dw || contentH != dh) new Span<uint>(d, dw).Fill(0xFF000000u);
            int sourceY = y - topOffset;
            if (sourceY < 0 || sourceY >= contentH) return;
            var r0 = (uint*)(src + (long)Y0[sourceY] * srcStride);
            var r1 = (uint*)(src + (long)Y1[sourceY] * srcStride);
            int fy = YW[sourceY], iy = 256 - fy;
            for (int x = 0; x < contentW; x++)
            {
                int x0 = X0[x], x1 = X1[x], fx = XW[x], ix = 256 - fx;
                uint a = r0[x0], b = r0[x1], c = r1[x0], e = r1[x1];
                uint rb = (((a & 0xFF00FFu) * (uint)ix + (b & 0xFF00FFu) * (uint)fx) >> 8) & 0xFF00FFu;
                uint ga = (((a >> 8) & 0xFF00FFu) * (uint)ix + ((b >> 8) & 0xFF00FFu) * (uint)fx) & 0xFF00FF00u;
                uint top = rb | ga;
                uint rb2 = (((c & 0xFF00FFu) * (uint)ix + (e & 0xFF00FFu) * (uint)fx) >> 8) & 0xFF00FFu;
                uint ga2 = (((c >> 8) & 0xFF00FFu) * (uint)ix + ((e >> 8) & 0xFF00FFu) * (uint)fx) & 0xFF00FF00u;
                uint bottom = rb2 | ga2;
                uint rbv = (((top & 0xFF00FFu) * (uint)iy + (bottom & 0xFF00FFu) * (uint)fy) >> 8) & 0xFF00FFu;
                uint gav = (((top >> 8) & 0xFF00FFu) * (uint)iy + ((bottom >> 8) & 0xFF00FFu) * (uint)fy) & 0xFF00FF00u;
                d[x + left] = Swap(rbv | gav);
            }
        });
    }

    private void BuildMaps(int sw, int sh, int dw, int dh)
    {
        if (sw == mapSrcW && sh == mapSrcH && dw == mapDstW && dh == mapDstH) return;
        double scale = Math.Max((double)dw / sw, (double)dh / sh);    // fill: cover the output, crop the rest
        double cropW = dw / scale, cropH = dh / scale;
        double offX = (sw - cropW) / 2, offY = (sh - cropH) / 2;
        (xs0, xs1, xw) = Axis(dw, sw, scale, offX);
        (ys0, ys1, yw) = Axis(dh, sh, scale, offY);
        (mapSrcW, mapSrcH, mapDstW, mapDstH) = (sw, sh, dw, dh);
    }

    private static (int[], int[], int[]) Axis(int dstLen, int srcLen, double scale, double offset)
    {
        var i0 = new int[dstLen]; var i1 = new int[dstLen]; var w = new int[dstLen];
        for (int i = 0; i < dstLen; i++)
        {
            double s = Math.Clamp((i + 0.5) / scale + offset - 0.5, 0, srcLen - 1);
            int a = (int)s;
            i0[i] = a;
            i1[i] = Math.Min(a + 1, srcLen - 1);
            w[i] = (int)((s - a) * 256);
        }
        return (i0, i1, w);
    }

    private static readonly ParallelOptions Options = new() { MaxDegreeOfParallelism = Math.Clamp(Environment.ProcessorCount / 2, 2, 8) };

    private bool EnsureOpen()
    {
        if (view != null) return true;
        long now = Environment.TickCount64;
        if (now < nextConnectAttempt) return false;
        nextConnectAttempt = now + 1000;

        if (mutex == 0) mutex = OpenMutexA(SYNCHRONIZE, false, "UnityCapture_Mutx");
        if (mutex == 0) return false;
        if (wantEvent == 0) wantEvent = CreateEventA(0, false, false, "UnityCapture_Want");
        if (sentEvent == 0) sentEvent = OpenEventA(EVENT_MODIFY_STATE, false, "UnityCapture_Sent");
        if (sentEvent == 0) return false;
        if (mapping == 0) mapping = OpenFileMappingA(FILE_MAP_WRITE, false, "UnityCapture_Data");
        if (mapping == 0) return false;
        view = (byte*)MapViewOfFile(mapping, FILE_MAP_WRITE, 0, 0, 0);
        return view != null;
    }

    public void Reset() { lock (gate) CloseAll(); }

    private void CloseAll()
    {
        if (view != null) { UnmapViewOfFile((nint)view); view = null; }
        foreach (var h in new[] { mapping, sentEvent, wantEvent, mutex }) if (h != 0) CloseHandle(h);
        mapping = sentEvent = wantEvent = mutex = 0;
        nextConnectAttempt = 0;
    }

    public void Dispose() { lock (gate) CloseAll(); }

    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Ansi)] private static extern nint OpenMutexA(uint access, bool inherit, string name);
    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Ansi)] private static extern nint CreateEventA(nint attributes, bool manualReset, bool initialState, string name);
    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Ansi)] private static extern nint OpenEventA(uint access, bool inherit, string name);
    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Ansi)] private static extern nint OpenFileMappingA(uint access, bool inherit, string name);
    [DllImport("kernel32", SetLastError = true)] private static extern nint MapViewOfFile(nint mapping, uint access, uint offsetHigh, uint offsetLow, nuint bytes);
    [DllImport("kernel32", SetLastError = true)] private static extern bool UnmapViewOfFile(nint address);
    [DllImport("kernel32", SetLastError = true)] private static extern uint WaitForSingleObject(nint handle, uint milliseconds);
    [DllImport("kernel32", SetLastError = true)] private static extern bool ReleaseMutex(nint handle);
    [DllImport("kernel32", SetLastError = true)] private static extern bool SetEvent(nint handle);
    [DllImport("kernel32", SetLastError = true)] private static extern bool CloseHandle(nint handle);
}
