using System.Text.Json;

namespace FaceLive.Services;

public sealed record ReferenceItem(string Name, string Mode, string? Thumbnail, string Url, bool IsUserImage = false);

public static class PresetCatalog
{
    public static readonly IReadOnlyDictionary<string, string> ModeNames = new Dictionary<string, string>
    {
        ["charx"] = "换人物",
        ["clothx"] = "换衣服",
        ["vibex"] = "换风格",
    };

    /// <summary>Official prompts from the Xmax SDK README for each mode.</summary>
    public static readonly IReadOnlyDictionary<string, string> DefaultPrompts = new Dictionary<string, string>
    {
        ["charx"] = "视频中角色替换成参考图中角色",
        ["clothx"] = "视频中人物衣服替换成参考图中衣服",
        ["vibex"] = "视频风格变为参考图指定的风格",
    };

    private static readonly Dictionary<string, string> ChineseNames = new()
    {
        ["Classical Girl"] = "古风女孩", ["Classical Boy"] = "古风男孩", ["Modern Girl"] = "现代女孩", ["Modern Boy"] = "现代男孩",
        ["Blonde Girl"] = "金发女孩", ["Curly-Haired Boy"] = "卷发男孩", ["Anime Boy"] = "动漫男孩", ["Anime Girl"] = "动漫女孩",
        ["Alien Kitty"] = "外星猫", ["Bronze Figure"] = "青铜人像", ["3D Cow"] = "3D 奶牛", ["3D Milk Frog"] = "3D 牛蛙",
        ["3D Kangaroo"] = "3D 袋鼠", ["Mecha Girl"] = "机甲少女", ["Mecha Warrior"] = "机甲战士", ["Special Ops"] = "特种兵",
        ["Humanoid Robot"] = "人形机器人", ["Chill Capybara"] = "卡皮巴拉", ["Gentle Panda"] = "熊猫", ["Cute Hamster"] = "仓鼠",
        ["Illustrated Girl"] = "插画女孩", ["Cartoon Boy"] = "卡通男孩", ["Business Milk Frog"] = "商务牛蛙", ["Corporate Cow"] = "职场奶牛",
    };

    public static IReadOnlyList<ReferenceItem> Load()
    {
        using var stream = typeof(PresetCatalog).Assembly.GetManifestResourceStream("FaceLive.presets.json")!;
        using var doc = JsonDocument.Parse(stream);
        var list = new List<ReferenceItem>();
        foreach (var item in doc.RootElement.EnumerateArray())
        {
            string name = item.GetProperty("name").GetString()!;
            // WPF cannot decode WebP on every Windows 10 install, so ask the CDN for JPEG thumbnails.
            string thumb = item.GetProperty("thumbnail").GetString()!.Replace("format/webp", "format/jpg");
            list.Add(new(ChineseNames.GetValueOrDefault(name, name), item.GetProperty("mode").GetString()!,
                thumb, item.GetProperty("reference").GetString()!));
        }
        return list;
    }
}
