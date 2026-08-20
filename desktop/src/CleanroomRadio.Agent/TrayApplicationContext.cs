using System.Diagnostics;
using System.Drawing;
using System.Windows.Forms;
using CleanroomRadio.Desktop.Common;

namespace CleanroomRadio.Desktop.Agent;

public sealed class TrayApplicationContext : ApplicationContext, ITrayShellPresenter
{
    private readonly NotifyIcon _trayIcon;
    private readonly System.Windows.Forms.Timer _startupTimer;
    private readonly TrayAgentController _controller;
    private readonly TrayMenuModel _trayMenuModel;
    private Process? _shellProcess;

    public TrayApplicationContext()
        : this(
            new BackendLifecycleController(
                new BackendOptions(
                    Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "RadioTEDU-OnAir-Backend.exe")),
                    Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                        "RadioTEDU",
                        "OnAir"),
                    8100),
                new ProcessBackendGateway(),
                new ProcessBackendHealthProbe()),
            new MessageBoxShutdownConfirmation())
    {
    }

    internal TrayApplicationContext(IBackendLifecycle backendLifecycle, IShutdownConfirmation shutdownConfirmation)
    {
        _trayMenuModel = TrayMenuModel.BuildDefault();
        _controller = new TrayAgentController(
            backendLifecycle ?? throw new ArgumentNullException(nameof(backendLifecycle)),
            this,
            shutdownConfirmation ?? throw new ArgumentNullException(nameof(shutdownConfirmation)));

        _trayIcon = new NotifyIcon
        {
            Icon = SystemIcons.Application,
            Text = "RadioTEDU OnAir",
            Visible = true,
        };
        _trayIcon.ContextMenuStrip = BuildTrayMenu();

        _startupTimer = new System.Windows.Forms.Timer
        {
            Interval = 1,
            Enabled = true,
        };
        _startupTimer.Tick += HandleStartupTimerTick;
    }

    public void ShowPanel()
    {
        if (_shellProcess is not null && !_shellProcess.HasExited)
        {
            return;
        }

        var shellPath = ResolveShellExecutablePath();
        var startInfo = new ProcessStartInfo
        {
            FileName = shellPath,
            WorkingDirectory = Path.GetDirectoryName(shellPath) ?? AppContext.BaseDirectory,
            UseShellExecute = false,
        };

        startInfo.Environment["CLEANROOM_HOST"] = Environment.GetEnvironmentVariable("CLEANROOM_HOST") ?? "127.0.0.1";
        startInfo.Environment["CLEANROOM_PORT"] = Environment.GetEnvironmentVariable("CLEANROOM_PORT") ?? "8100";
        startInfo.Environment["CLEANROOM_STATION_ID"] = Environment.GetEnvironmentVariable("CLEANROOM_STATION_ID") ?? "0";

        _shellProcess = Process.Start(startInfo)
            ?? throw new InvalidOperationException("RadioTEDU OnAir shell could not be started.");
    }

    public void RequestClose(CloseIntent intent)
    {
        if (intent != CloseIntent.ExitApplication || _shellProcess is null || _shellProcess.HasExited)
        {
            return;
        }

        try
        {
            if (!_shellProcess.CloseMainWindow() || !_shellProcess.WaitForExit(2000))
            {
                _shellProcess.Kill(entireProcessTree: true);
            }
        }
        catch
        {
            // The shell may already be gone; shutdown should remain best-effort.
        }
    }

    private static string ResolveShellExecutablePath()
    {
        var candidate = Path.GetFullPath(Path.Combine(
            AppContext.BaseDirectory,
            "shell",
            "RadioTEDU-OnAir.exe"));
        if (File.Exists(candidate))
        {
            return candidate;
        }

        candidate = Path.GetFullPath(Path.Combine(
            AppContext.BaseDirectory,
            "RadioTEDU-OnAir.exe"));
        if (File.Exists(candidate))
        {
            return candidate;
        }

        throw new FileNotFoundException("RadioTEDU OnAir shell executable was not found.", candidate);
    }

    private ContextMenuStrip BuildTrayMenu()
    {
        var menu = new ContextMenuStrip();

        foreach (var item in _trayMenuModel.Items)
        {
            var menuItem = new ToolStripMenuItem(item.Text)
            {
                Tag = item.Command,
            };

            menuItem.Click += HandleTrayMenuItemClick;
            menu.Items.Add(menuItem);
        }

        return menu;
    }

    private async void HandleStartupTimerTick(object? sender, EventArgs e)
    {
        _startupTimer.Stop();
        _startupTimer.Tick -= HandleStartupTimerTick;

        var result = await _controller.InitializeAsync().ConfigureAwait(true);
        if (!result.Succeeded && result.ErrorMessage is not null)
        {
            ShowError(result.ErrorMessage);
        }
    }

    private async void HandleTrayMenuItemClick(object? sender, EventArgs e)
    {
        if (sender is not ToolStripMenuItem menuItem || menuItem.Tag is not TrayCommand command)
        {
            return;
        }

        try
        {
            switch (command)
            {
                case TrayCommand.OpenPanel:
                case TrayCommand.RestartBackend:
                    {
                        var result = await _controller.HandleCommandAsync(command).ConfigureAwait(true);
                        if (!result.Succeeded && result.ErrorMessage is not null)
                        {
                            ShowError(result.ErrorMessage);
                        }

                        break;
                    }
                case TrayCommand.StopBroadcastAndExit:
                    {
                        var result = await _controller.RequestShutdownAsync().ConfigureAwait(true);
                        if (result.Succeeded && result.ShouldExit)
                        {
                            ExitThread();
                            return;
                        }

                        if (result.ErrorMessage is not null)
                        {
                            ShowError(result.ErrorMessage);
                        }

                        break;
                    }
            }
        }
        catch (Exception exception)
        {
            ShowError(exception.Message);
        }
    }

    private void ShowError(string message)
    {
        MessageBox.Show(
            message,
            "RadioTEDU OnAir",
            MessageBoxButtons.OK,
            MessageBoxIcon.Error);
    }

    protected override void ExitThreadCore()
    {
        _startupTimer.Stop();
        _startupTimer.Dispose();
        _trayIcon.Visible = false;
        _trayIcon.Dispose();

        if (_shellProcess is not null)
        {
            RequestClose(CloseIntent.ExitApplication);
            _shellProcess.Dispose();
        }

        base.ExitThreadCore();
    }
}
