using System.Windows;
using System.Windows.Input;
using System.Windows.Media;

namespace FaceLive;

public partial class GeneratedPreviewWindow : Window
{
    private bool fullscreen;
    private WindowState previousState;
    private Rect previousBounds;

    public event Action? StopRequested;

    public GeneratedPreviewWindow(Visual source)
    {
        InitializeComponent();
        // Reuse the already-rendered video visual. SDK video sources are single-consumer
        // mailboxes: attaching another video control would steal frames from the main view.
        PreviewBrush.Visual = source;
        Closed += (_, _) => PreviewBrush.Visual = null;
    }

    public void UpdateState(bool generating, uint width, uint height)
    {
        bool hasFrame = width > 0 && height > 0;
        Placeholder.Visibility = hasFrame ? Visibility.Collapsed : Visibility.Visible;
        StateText.Text = generating ? "● 生成中" : hasFrame ? "已停止 · 保留最后一帧" : "等待生成";
        StateText.Foreground = (Brush)FindResource(generating ? "Ok" : "Muted");
        DimensionsText.Text = hasFrame ? $"生成画面 {width} × {height} · 等比显示" : "独立大屏预览";
        StopButton.IsEnabled = generating;
    }

    private void ToggleFullscreen()
    {
        if (!fullscreen)
        {
            previousState = WindowState;
            previousBounds = RestoreBounds;
            WindowState = WindowState.Normal;
            WindowStyle = WindowStyle.None;
            ResizeMode = ResizeMode.NoResize;
            WindowState = WindowState.Maximized;
            HeaderRow.Height = FooterRow.Height = new GridLength(0);
            fullscreen = true;
        }
        else
        {
            WindowState = WindowState.Normal;
            WindowStyle = WindowStyle.SingleBorderWindow;
            ResizeMode = ResizeMode.CanResize;
            if (!previousBounds.IsEmpty && previousBounds.Width > 0 && previousBounds.Height > 0)
            {
                Left = previousBounds.Left; Top = previousBounds.Top;
                Width = previousBounds.Width; Height = previousBounds.Height;
            }
            WindowState = previousState;
            HeaderRow.Height = new GridLength(56);
            FooterRow.Height = new GridLength(34);
            fullscreen = false;
        }
    }

    private void FullscreenButton_Click(object sender, RoutedEventArgs e) => ToggleFullscreen();

    private void Preview_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ClickCount != 2) return;
        ToggleFullscreen();
        e.Handled = true;
    }

    private void Window_PreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.F11 || (e.Key == Key.Escape && fullscreen))
        {
            ToggleFullscreen();
            e.Handled = true;
        }
    }

    private void StopButton_Click(object sender, RoutedEventArgs e) => StopRequested?.Invoke();
}
