using System.Windows;
using System.Windows.Controls;

namespace FaceLive.Controls;

/// <summary>Centers a preview canvas at its actual video aspect ratio inside the available space.</summary>
public sealed class AspectRatioBox : Decorator
{
    public static readonly DependencyProperty AspectRatioProperty = DependencyProperty.Register(
        nameof(AspectRatio), typeof(double), typeof(AspectRatioBox),
        new FrameworkPropertyMetadata(9d / 16, FrameworkPropertyMetadataOptions.AffectsMeasure | FrameworkPropertyMetadataOptions.AffectsArrange),
        value => value is double ratio && double.IsFinite(ratio) && ratio > 0);

    public double AspectRatio
    {
        get => (double)GetValue(AspectRatioProperty);
        set => SetValue(AspectRatioProperty, value);
    }

    private Size Fit(Size available)
    {
        double width = double.IsFinite(available.Width) ? available.Width : 640;
        double height = double.IsFinite(available.Height) ? available.Height : width / AspectRatio;
        width = Math.Max(0, Math.Min(width, height * AspectRatio));
        return new Size(width, width / AspectRatio);
    }

    protected override Size MeasureOverride(Size constraint)
    {
        var size = Fit(constraint);
        Child?.Measure(size);
        return size;
    }

    protected override Size ArrangeOverride(Size arrangeSize)
    {
        var size = Fit(arrangeSize);
        Child?.Arrange(new Rect((arrangeSize.Width - size.Width) / 2, (arrangeSize.Height - size.Height) / 2, size.Width, size.Height));
        return arrangeSize;
    }
}
