using System.Windows;
using System.Windows.Threading;

namespace FaceLive;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        DispatcherUnhandledException += OnUnhandled;
        base.OnStartup(e);
    }

    private static void OnUnhandled(object sender, DispatcherUnhandledExceptionEventArgs e)
    {
        MessageBox.Show(e.Exception.Message, "换脸直播助手 - 出错了", MessageBoxButton.OK, MessageBoxImage.Error);
        e.Handled = true;
    }
}
