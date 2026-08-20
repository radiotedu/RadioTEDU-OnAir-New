using System.Collections.Concurrent;
using System.Diagnostics;
using Microsoft.Extensions.Hosting;

namespace CleanroomRadio.ServiceHost;

public sealed class ServiceSupervisor : BackgroundService
{
    private readonly ServiceHostSettings _settings;
    private readonly RedactingRollingLog _log;
    private readonly ConcurrentDictionary<string, Process> _activeProcesses = new(StringComparer.OrdinalIgnoreCase);
    private readonly ConcurrentDictionary<string, ChildProcessState> _childStates = new(StringComparer.OrdinalIgnoreCase);
    private ServiceStateWriter? _stateWriter;

    public ServiceSupervisor(ServiceHostSettings settings, RedactingRollingLog log)
    {
        _settings = settings;
        _log = log;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        IReadOnlyList<ManagedProcessDefinition> definitions;
        try
        {
            definitions = ServiceHostSettings.ReadDefinitions(_settings.ConfigPath);
        }
        catch (Exception exception)
        {
            _log.Write("error", $"Configuration could not be loaded: {exception.Message}");
            throw;
        }

        PendingRecoveryApplier.TryApply(_settings, _log);

        _stateWriter = new ServiceStateWriter(_settings);
        foreach (var definition in definitions)
        {
            _childStates[definition.Id] = new ChildProcessState(
                definition.Id,
                "Starting",
                null,
                0,
                null,
                null,
                null);
        }

        await PublishStateAsync("Starting", CancellationToken.None).ConfigureAwait(false);
        _log.Write("information", $"Service host started with {definitions.Count} configured child process(es).");

        var workers = definitions.Select(definition => SuperviseAsync(definition, stoppingToken));
        await Task.WhenAll(workers).ConfigureAwait(false);
    }

    public override async Task StopAsync(CancellationToken cancellationToken)
    {
        _log.Write("information", "Stop requested; terminating managed process trees.");
        var processes = _activeProcesses.ToArray();
        foreach (var (_, process) in processes)
        {
            TryTerminateProcessTree(process);
        }

        await Task.WhenAll(processes.Select(pair => WaitForExitAsync(pair.Value, cancellationToken))).ConfigureAwait(false);
        await base.StopAsync(cancellationToken).ConfigureAwait(false);
        await PublishStateAsync("Stopped", CancellationToken.None).ConfigureAwait(false);
    }

    private async Task SuperviseAsync(ManagedProcessDefinition definition, CancellationToken stoppingToken)
    {
        var backoff = new RestartBackoff();
        var restartCount = 0;

        while (!stoppingToken.IsCancellationRequested)
        {
            Process? process = null;
            try
            {
                process = StartProcess(definition);
                var startedUtc = DateTimeOffset.UtcNow;
                if (process.HasExited)
                {
                    throw new InvalidOperationException($"Managed process '{definition.Id}' exited immediately.");
                }

                _activeProcesses[definition.Id] = process;
                UpdateChild(definition.Id, "Running", process.Id, restartCount, startedUtc, null, null);
                await PublishStateAsync("Running", CancellationToken.None).ConfigureAwait(false);
                _log.Write("information", $"Managed process '{definition.Id}' started as PID {process.Id} ({Path.GetFileName(definition.ExecutablePath)}).");

                var standardOutput = DrainStreamAsync(process.StandardOutput, definition.Id, "information");
                var standardError = DrainStreamAsync(process.StandardError, definition.Id, "warning");
                await process.WaitForExitAsync(stoppingToken).ConfigureAwait(false);
                await Task.WhenAll(standardOutput, standardError).ConfigureAwait(false);

                var exitCode = process.ExitCode;
                if (!definition.RestartOnExit)
                {
                    UpdateChild(definition.Id, "Exited", null, restartCount, null, null, exitCode);
                    await PublishStateAsync("Running", CancellationToken.None).ConfigureAwait(false);
                    _log.Write("information", $"Managed process '{definition.Id}' exited with code {exitCode}; restart-on-exit is disabled.");
                    return;
                }

                var delay = backoff.RegisterExit(DateTimeOffset.UtcNow - startedUtc);
                restartCount++;
                UpdateChild(definition.Id, "BackingOff", null, restartCount, null, DateTimeOffset.UtcNow + delay, exitCode);
                await PublishStateAsync("Degraded", CancellationToken.None).ConfigureAwait(false);
                _log.Write("warning", $"Managed process '{definition.Id}' exited with code {exitCode}; restart scheduled after {delay.TotalSeconds:0} seconds.");
                await Task.Delay(delay, stoppingToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested)
            {
                break;
            }
            catch (Exception exception)
            {
                if (!definition.RestartOnExit)
                {
                    UpdateChild(definition.Id, "Exited", null, restartCount, null, null, null);
                    await PublishStateAsync("Running", CancellationToken.None).ConfigureAwait(false);
                    _log.Write("error", $"Managed process '{definition.Id}' failed and restart-on-exit is disabled: {exception.Message}");
                    return;
                }

                var delay = backoff.RegisterExit(TimeSpan.Zero);
                restartCount++;
                UpdateChild(definition.Id, "BackingOff", null, restartCount, null, DateTimeOffset.UtcNow + delay, null);
                await PublishStateAsync("Degraded", CancellationToken.None).ConfigureAwait(false);
                _log.Write("error", $"Managed process '{definition.Id}' failed: {exception.Message}. Restart scheduled after {delay.TotalSeconds:0} seconds.");
                await Task.Delay(delay, stoppingToken).ConfigureAwait(false);
            }
            finally
            {
                if (process is not null)
                {
                    _activeProcesses.TryRemove(definition.Id, out _);
                    process.Dispose();
                }
            }
        }

        UpdateChild(definition.Id, "Stopped", null, restartCount, null, null, null);
    }

    private static Process StartProcess(ManagedProcessDefinition definition)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = definition.ExecutablePath,
            Arguments = definition.Arguments,
            WorkingDirectory = definition.WorkingDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };

        return Process.Start(startInfo) ?? throw new InvalidOperationException($"Process '{definition.Id}' could not be started.");
    }

    private async Task DrainStreamAsync(StreamReader reader, string id, string level)
    {
        while (await reader.ReadLineAsync().ConfigureAwait(false) is { } line)
        {
            _log.Write(level, $"[{id}] {line}");
        }
    }

    private void UpdateChild(
        string id,
        string status,
        int? processId,
        int restartCount,
        DateTimeOffset? startedUtc,
        DateTimeOffset? nextRestartUtc,
        int? lastExitCode) =>
        _childStates[id] = new ChildProcessState(id, status, processId, restartCount, startedUtc, nextRestartUtc, lastExitCode);

    private async Task PublishStateAsync(string status, CancellationToken cancellationToken)
    {
        if (_stateWriter is null)
        {
            return;
        }

        try
        {
            var state = new ServiceHostState(_settings.ServiceName, status, DateTimeOffset.UtcNow, _childStates.Values.OrderBy(child => child.Id).ToArray());
            await _stateWriter.WriteAsync(state, cancellationToken).ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            _log.Write("error", $"Could not publish operator state: {exception.Message}");
        }
    }

    private void TryTerminateProcessTree(Process process)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception exception)
        {
            _log.Write("warning", $"Could not terminate managed process tree: {exception.Message}");
        }
    }

    private static async Task WaitForExitAsync(Process process, CancellationToken cancellationToken)
    {
        try
        {
            if (!process.HasExited)
            {
                await process.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
            }
        }
        catch (InvalidOperationException)
        {
            // A process can exit between HasExited and WaitForExitAsync.
        }
    }
}
