namespace CleanroomRadio.Desktop.Agent;

public enum CloseIntent
{
    HideToTray,
    ExitApplication,
}

public interface ITrayShellPresenter
{
    void ShowPanel();

    void RequestClose(CloseIntent intent);
}

public sealed class TrayAgentController
{
    private readonly IBackendLifecycle _backendLifecycle;
    private readonly ITrayShellPresenter _shellPresenter;
    private readonly IShutdownConfirmation _shutdownConfirmation;

    public TrayAgentController(
        IBackendLifecycle backendLifecycle,
        ITrayShellPresenter shellPresenter,
        IShutdownConfirmation shutdownConfirmation)
    {
        _backendLifecycle = backendLifecycle ?? throw new ArgumentNullException(nameof(backendLifecycle));
        _shellPresenter = shellPresenter ?? throw new ArgumentNullException(nameof(shellPresenter));
        _shutdownConfirmation = shutdownConfirmation ?? throw new ArgumentNullException(nameof(shutdownConfirmation));
    }

    public async Task<LifecycleOperationResult> InitializeAsync()
    {
        var result = await _backendLifecycle.EnsureRunningAsync().ConfigureAwait(true);
        if (result.Succeeded)
        {
            _shellPresenter.ShowPanel();
        }

        return result;
    }

    public async Task<LifecycleOperationResult> HandleCommandAsync(TrayCommand command)
    {
        return command switch
        {
            TrayCommand.OpenPanel => await OpenPanelAsync().ConfigureAwait(true),
            TrayCommand.RestartBackend => await RestartBackendAsync().ConfigureAwait(true),
            TrayCommand.StopBroadcastAndExit => (await RequestShutdownAsync().ConfigureAwait(true)).ToLifecycleResult(),
            _ => throw new ArgumentOutOfRangeException(nameof(command), command, "Unknown tray command."),
        };
    }

    public async Task<ShutdownOperationResult> RequestShutdownAsync()
    {
        if (!_shutdownConfirmation.ConfirmStopBroadcastAndExit())
        {
            return ShutdownOperationResult.Cancelled();
        }

        var stopResult = await _backendLifecycle.StopAsync().ConfigureAwait(true);
        if (!stopResult.Succeeded)
        {
            return ShutdownOperationResult.Failure(stopResult.ErrorMessage!);
        }

        _shellPresenter.RequestClose(CloseIntent.ExitApplication);
        return ShutdownOperationResult.Success();
    }

    private async Task<LifecycleOperationResult> OpenPanelAsync()
    {
        var result = await _backendLifecycle.EnsureRunningAsync().ConfigureAwait(true);
        if (result.Succeeded)
        {
            _shellPresenter.ShowPanel();
        }

        return result;
    }

    private async Task<LifecycleOperationResult> RestartBackendAsync()
    {
        var result = await _backendLifecycle.RestartAsync().ConfigureAwait(true);
        if (result.Succeeded)
        {
            _shellPresenter.ShowPanel();
        }

        return result;
    }
}

internal static class ShutdownOperationResultExtensions
{
    public static LifecycleOperationResult ToLifecycleResult(this ShutdownOperationResult result)
    {
        return result.Succeeded
            ? LifecycleOperationResult.Success()
            : (result.ErrorMessage is null
                ? LifecycleOperationResult.Failure("Shutdown was cancelled.")
                : LifecycleOperationResult.Failure(result.ErrorMessage));
    }
}
