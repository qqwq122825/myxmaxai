using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace FaceLive.Services;

public sealed class AppSettings
{
    public string ProtectedApiKey { get; set; } = "";
    public bool GlobalEnvironment { get; set; } = true;
    public string CameraId { get; set; } = "";
    public string Mode { get; set; } = "charx";
    public string ReferenceUrl { get; set; } = "";
    public string ReferenceName { get; set; } = "";
    public string CustomPrompt { get; set; } = "";
    public bool FaceRestoration { get; set; }
    public bool MirrorOutput { get; set; }
    public bool FlipVertical { get; set; } = true;
    public int AutoStopMinutes { get; set; }
    public int OutputWidth { get; set; } = 720;
    public int OutputHeight { get; set; } = 1280;
    public List<UserReference> UserReferences { get; set; } = [];

    private static string FilePath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "FaceLive", "settings.json");

    /// <summary>The API key, encrypted with Windows DPAPI for the current user.</summary>
    public string ApiKey
    {
        get
        {
            if (string.IsNullOrEmpty(ProtectedApiKey)) return "";
            try
            {
                var bytes = ProtectedData.Unprotect(Convert.FromBase64String(ProtectedApiKey), null, DataProtectionScope.CurrentUser);
                return Encoding.UTF8.GetString(bytes);
            }
            catch { return ""; }
        }
        set => ProtectedApiKey = string.IsNullOrEmpty(value) ? "" : Convert.ToBase64String(
            ProtectedData.Protect(Encoding.UTF8.GetBytes(value), null, DataProtectionScope.CurrentUser));
    }

    public static AppSettings Load()
    {
        try
        {
            if (File.Exists(FilePath))
                return JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(FilePath)) ?? new();
        }
        catch { }
        return new();
    }

    public void Save()
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(FilePath)!);
            File.WriteAllText(FilePath, JsonSerializer.Serialize(this, new JsonSerializerOptions { WriteIndented = true }));
        }
        catch { }
    }
}

public sealed record UserReference(string Name, string Url, string Mode);
