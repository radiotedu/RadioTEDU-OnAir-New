using CleanroomRadio.Desktop.Common;

namespace CleanroomRadio.Desktop.Agent;

public interface IBackendLifecycle
{
    Task<LifecycleOperationResult> EnsureRunningAsync();

    Task<LifecycleOperationResult> RestartAsync();

    Task<LifecycleOperationResult> StopAsync();
}

public sealed class BackendLifecycleController : IBackendLifecycle
{
    private static readonly TimeSpan GracefulStopTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan StartupHealthDelay = TimeSpan.FromMilliseconds(500);
    private const int StartupHealthRetries = 240;

    private readonly BackendOptions _options;
    private readonly IBackendProcessGateway _processGateway;
    private readonly IBackendHealthProbe _healthProbe;
    private readonly Uri _baseAddress;
    private IBackendProcessHandle? _ownedProcess;

    public BackendLifecycleController(
        BackendOptions options,
        IBackendProcessGateway processGateway,
        IBackendHealthProbe healthProbe)
    {
        _options = options ?? throw new ArgumentNullException(nameof(options));
        _processGateway = processGateway ?? throw new ArgumentNullException(nameof(processGateway));
        _healthProbe = healthProbe ?? throw new ArgumentNullException(nameof(healthProbe));
        _baseAddress = new Uri($"http://127.0.0.1:{_options.Port}/");
    }

    public async Task<LifecycleOperationResult> EnsureRunningAsync()
    {
        if (await _healthProbe.IsHealthyAsync(_baseAddress).ConfigureAwait(true))
        {
            AttachMatchingProcessIfAvailable();
            return LifecycleOperationResult.Success();
        }

        var trackedProcess = AttachMatchingProcessIfAvailable();
        if (trackedProcess is not null && !trackedProcess.HasExited)
        {
            var stop = StopHandle(trackedProcess);
            if (!stop.Succeeded)
            {
                return stop;
            }
        }

        var startedProcess = _processGateway.Start(_options);
        _ownedProcess = startedProcess;

        var ready = await _healthProbe.WaitForHealthyAsync(
            _baseAddress,
            retries: StartupHealthRetries,
            delay: StartupHealthDelay).ConfigureAwait(true);
        if (ready)
        {
            return LifecycleOperationResult.Success();
        }

        var stopResult = StopHandle(startedProcess);
        if (!stopResult.Succeeded)
        {
            return stopResult;
        }

        return LifecycleOperationResult.Failure("RadioTEDU OnAir backend did not become healthy.");
    }

    public async Task<LifecycleOperationResult> RestartAsync()
    {
        var trackedProcess = AttachMatchingProcessIfAvailable();
        if (trackedProcess is null)
        {
            if (await _healthProbe.IsHealthyAsync(_baseAddress).ConfigureAwait(true))
            {
                return LifecycleOperationResult.Failure(
                    "A RadioTEDU OnAir backend is already running, but the agent does not own it. Restart cannot proceed.");
            }

            return await StartFreshAsync().ConfigureAwait(true);
        }

        var stopResult = StopHandle(trackedProcess);
        if (!stopResult.Succeeded)
        {
            return stopResult;
        }

        if (await _healthProbe.IsHealthyAsync(_baseAddress).ConfigureAwait(true))
        {
            return LifecycleOperationResult.Failure(
                "RadioTEDU OnAir backend is still running after stop. Restart cannot proceed.");
        }

        return await StartFreshAsync().ConfigureAwait(true);
    }

    public async Task<LifecycleOperationResult> StopAsync()
    {
        var trackedProcess = AttachMatchingProcessIfAvailable();
        if (trackedProcess is null)
        {
            if (await _healthProbe.IsHealthyAsync(_baseAddress).ConfigureAwait(true))
            {
                return LifecycleOperationResult.Failure(
                    "A RadioTEDU OnAir backend is running, but the agent does not own it. Stop cannot proceed.");
            }

            return LifecycleOperationResult.Success();
        }

        var stopResult = StopHandle(trackedProcess);
        if (!stopResult.Succeeded)
        {
            return stopResult;
        }

        if (await _healthProbe.IsHealthyAsync(_baseAddress).ConfigureAwait(true))
        {
            return LifecycleOperationResult.Failure(
                "RadioTEDU OnAir backend is still running after stop.");
        }

        return LifecycleOperationResult.Success();
    }

    private async Task<LifecycleOperationResult> StartFreshAsync()
    {
        var startedProcess = _processGateway.Start(_options);
        _ownedProcess = startedProcess;

        var ready = await _healthProbe.WaitForHealthyAsync(
            _baseAddress,
            retries: StartupHealthRetries,
            delay: StartupHealthDelay).ConfigureAwait(true);
        if (ready)
        {
            return LifecycleOperationResult.Success();
        }

        var stopResult = StopHandle(startedProcess);
        if (!stopResult.Succeeded)
        {
            return stopResult;
        }

        return LifecycleOperationResult.Failure("RadioTEDU OnAir backend did not become healthy.");
    }

    private IBackendProcessHandle? AttachMatchingProcessIfAvailable()
    {
        if (_ownedProcess is not null && !_ownedProcess.HasExited)
        {
            return _ownedProcess;
        }

        _ownedProcess?.Dispose();
        _ownedProcess = _processGateway.FindMatchingProcess(_options.ExecutablePath);
        return _ownedProcess;
    }

    private LifecycleOperationResult StopHandle(IBackendProcessHandle processHandle)
    {
        try
        {
            if (!processHandle.TryGracefulStop(GracefulStopTimeout))
            {
                processHandle.ForceKill();
            }
        }
        catch (Exception exception)
        {
            return LifecycleOperationResult.Failure(
                $"RadioTEDU OnAir backend could not be stopped.\n\n{exception.Message}");
        }
        finally
        {
            if (ReferenceEquals(_ownedProcess, processHandle))
            {
                _ownedProcess = null;
            }

            processHandle.Dispose();
        }

        return LifecycleOperationResult.Success();
    }
}
