using System.Diagnostics;
using System.IO;
using Microsoft.Win32;

namespace FaceLive.Services;

/// <summary>
/// Registers / unregisters the bundled Unity Capture DirectShow filter as a webcam device.
/// The DLLs are copied to %ProgramData% first so the device keeps working if the app folder moves.
/// </summary>
public static class VirtualCameraInstaller
{
    public const string DeviceName = "FaceLive Camera";
    private const string Clsid64 = "{5C2CD55C-92AD-4999-8666-912BD3E70010}";

    private static string SourceDir => Path.Combine(AppContext.BaseDirectory, "VirtualCam");
    private static string InstallDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "FaceLive", "VirtualCam");

    public static bool IsInstalled()
    {
        using var key = Registry.ClassesRoot.OpenSubKey($@"CLSID\{Clsid64}\InprocServer32");
        var path = key?.GetValue(null) as string;
        return !string.IsNullOrEmpty(path) && File.Exists(path);
    }

    public static Task<bool> InstallAsync() => RunElevatedAsync(install: true);
    public static Task<bool> UninstallAsync() => RunElevatedAsync(install: false);

    private static async Task<bool> RunElevatedAsync(bool install)
    {
        string dll64 = Path.Combine(InstallDir, "UnityCaptureFilter64.dll");
        string dll32 = Path.Combine(InstallDir, "UnityCaptureFilter32.dll");
        string script = Path.Combine(Path.GetTempPath(), $"facelive-vcam-{Guid.NewGuid():N}.cmd");

        var lines = new List<string> { "@echo off", "chcp 65001 >nul" };
        if (install)
        {
            lines.Add($"mkdir \"{InstallDir}\" 2>nul");
            lines.Add($"copy /Y \"{Path.Combine(SourceDir, "UnityCaptureFilter64.dll")}\" \"{dll64}\" >nul");
            lines.Add($"copy /Y \"{Path.Combine(SourceDir, "UnityCaptureFilter32.dll")}\" \"{dll32}\" >nul");
            lines.Add($"regsvr32 /s \"{dll64}\" \"/i:UnityCaptureName={DeviceName}\"");
            lines.Add($"regsvr32 /s \"{dll32}\" \"/i:UnityCaptureName={DeviceName}\"");
        }
        else
        {
            lines.Add($"regsvr32 /s /u \"{dll64}\"");
            lines.Add($"regsvr32 /s /u \"{dll32}\"");
        }
        await File.WriteAllLinesAsync(script, lines);

        try
        {
            using var process = Process.Start(new ProcessStartInfo("cmd.exe", $"/c \"{script}\"")
            {
                UseShellExecute = true,
                Verb = "runas",               // one UAC prompt for the whole script
                WindowStyle = ProcessWindowStyle.Hidden,
            });
            if (process is null) return false;
            await process.WaitForExitAsync();
        }
        catch (System.ComponentModel.Win32Exception)
        {
            return false;                     // user declined the UAC prompt
        }
        finally
        {
            try { File.Delete(script); } catch { }
        }
        return install ? IsInstalled() : !IsInstalled();
    }
}
