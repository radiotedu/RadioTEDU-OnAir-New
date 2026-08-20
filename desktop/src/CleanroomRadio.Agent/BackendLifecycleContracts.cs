using System.Diagnostics;
using CleanroomRadio.Desktop.Common;

namespace CleanroomRadio.Desktop.Agent;

public interface IBackendProcessHandle : IDisposable
{
    bool HasExited { get; }

    bool TryGracefulStop(TimeSpan gracefulTimeout);

    void ForceKill();
}

public interface IProcessControl : IDisposable
{
    bool HasExited { get; }

    bool CloseMainWindow();

    bool WaitForExit(TimeSpan timeout);

    void Kill(bool entireProcessTree);
}

public interface IBackendProcessGateway
{
    IBackendProcessHandle? FindMatchingProcess(string executablePath);

    IBackendProcessHandle Start(BackendOptions options);
}

public interface IBackendHealthProbe
{
    Task<bool> IsHealthyAsync(Uri baseAddress, CancellationToken cancellationToken = default);

    Task<bool> WaitForHealthyAsync(
        Uri baseAddress,
        int retries = 30,
        TimeSpan? delay = null,
        CancellationToken cancellationToken = default);
}

public sealed class ProcessBackendHealthProbe : IBackendHealthProbe
{
    public Task<bool> IsHealthyAsync(Uri baseAddress, CancellationToken cancellationToken = default)
    {
        return HealthProbe.WaitForHealthyAsync(
            baseAddress,
            retries: 1,
            delay: TimeSpan.Zero,
            cancellationToken: cancellationToken);
    }

    public Task<bool> WaitForHealthyAsync(
        Uri baseAddress,
        int retries = 30,
        TimeSpan? delay = null,
        CancellationToken cancellationToken = default)
    {
        return HealthProbe.WaitForHealthyAsync(
            baseAddress,
            retries: retries,
            delay: delay,
            cancellationToken: cancellationToken);
    }
}

public sealed class ProcessBackendGateway : IBackendProcessGateway
{
    public IBackendProcessHandle? FindMatchingProcess(string executablePath)
    {
        var normalizedExecutablePath = Path.GetFullPath(executablePath);

        foreach (var process in Process.GetProcesses())
        {
            try
            {
                var processPath = process.MainModule?.FileName;
                if (string.IsNullOrWhiteSpace(processPath))
                {
                    process.Dispose();
                    continue;
                }

                if (!string.Equals(
                        Path.GetFullPath(processPath),
                        normalizedExecutablePath,
                        StringComparison.OrdinalIgnoreCase))
                {
                    process.Dispose();
                    continue;
                }

                return new ProcessBackendProcessHandle(new ProcessControl(process));
            }
            catch (Exception)
            {
                process.Dispose();
            }
        }

        return null;
    }

    public IBackendProcessHandle Start(BackendOptions options)
    {
        var startInfo = BackendProcessManager.CreateStartInfo(options);
        var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("RadioTEDU OnAir backend could not be started.");

        return new ProcessBackendProcessHandle(new ProcessControl(process));
    }
}

public sealed class ProcessControl : IProcessControl
{
    private readonly Process _process;

    public ProcessControl(Process process)
    {
        _process = process ?? throw new ArgumentNullException(nameof(process));
    }

    public bool HasExited => _process.HasExited;

    public bool CloseMainWindow()
    {
        return _process.CloseMainWindow();
    }

    public bool WaitForExit(TimeSpan timeout)
    {
        return _process.WaitForExit(timeout);
    }

    public void Kill(bool entireProcessTree)
    {
        _process.Kill(entireProcessTree);
    }

    public void Dispose()
    {
        _process.Dispose();
    }
}

public sealed class ProcessBackendProcessHandle : IBackendProcessHandle
{
    private readonly IProcessControl _processControl;

    public ProcessBackendProcessHandle(IProcessControl processControl)
    {
        _processControl = processControl ?? throw new ArgumentNullException(nameof(processControl));
    }

    public bool HasExited => _processControl.HasExited;

    public bool TryGracefulStop(TimeSpan gracefulTimeout)
    {
        if (_processControl.HasExited)
        {
            return true;
        }

        _processControl.CloseMainWindow();
        return _processControl.WaitForExit(gracefulTimeout);
    }

    public void ForceKill()
    {
        if (_processControl.HasExited)
        {
            return;
        }

        _processControl.Kill(entireProcessTree: true);
        _processControl.WaitForExit(TimeSpan.FromSeconds(5));
    }

    public void Dispose()
    {
        _processControl.Dispose();
    }
}
