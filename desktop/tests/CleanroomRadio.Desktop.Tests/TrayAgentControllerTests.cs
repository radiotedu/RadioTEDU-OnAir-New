using System.Windows.Forms;
using CleanroomRadio.Desktop.Agent;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class TrayAgentControllerTests
{
    [Fact]
    public async Task InitializeAsync_ShowsPanelAfterBackendStarts()
    {
        var backend = new FakeTrayBackendLifecycle(
            LifecycleOperationResult.Success());
        var shell = new FakeTrayShellPresenter();
        var controller = CreateController(backend, shell, confirmation: true);

        var result = await controller.InitializeAsync();

        Assert.True(result.Succeeded);
        Assert.True(shell.ShowPanelCalled);
        Assert.False(shell.RequestCloseCalled);
    }

    [Fact]
    public async Task RestartBackendAsync_SurfacesBackendFailureWithoutShowingThePanel()
    {
        var backend = new FakeTrayBackendLifecycle(
            LifecycleOperationResult.Failure("backend restart failed"));
        var shell = new FakeTrayShellPresenter();
        var controller = CreateController(backend, shell, confirmation: true);

        var result = await controller.HandleCommandAsync(TrayCommand.RestartBackend);

        Assert.False(result.Succeeded);
        Assert.Equal("backend restart failed", result.ErrorMessage);
        Assert.False(shell.ShowPanelCalled);
    }

    [Fact]
    public async Task RequestShutdownAsync_DoesNotExitWhenStopFails()
    {
        var backend = new FakeTrayBackendLifecycle(
            LifecycleOperationResult.Failure("backend still running"));
        var shell = new FakeTrayShellPresenter();
        var controller = CreateController(backend, shell, confirmation: true);

        var result = await controller.RequestShutdownAsync();

        Assert.False(result.Succeeded);
        Assert.False(result.ShouldExit);
        Assert.Equal("backend still running", result.ErrorMessage);
        Assert.False(shell.RequestCloseCalled);
    }

    [Fact]
    public async Task RequestShutdownAsync_RequestsShellCloseAfterConfirmationAndStopSucceed()
    {
        var backend = new FakeTrayBackendLifecycle(
            LifecycleOperationResult.Success());
        var shell = new FakeTrayShellPresenter();
        var controller = CreateController(backend, shell, confirmation: true);

        var result = await controller.RequestShutdownAsync();

        Assert.True(result.Succeeded);
        Assert.True(result.ShouldExit);
        Assert.Equal(CloseIntent.ExitApplication, shell.RequestCloseIntent);
    }

    private static TrayAgentController CreateController(
        FakeTrayBackendLifecycle backend,
        FakeTrayShellPresenter shell,
        bool confirmation)
    {
        return new TrayAgentController(
            backend,
            shell,
            new FakeShutdownConfirmation(confirmation));
    }

    private sealed class FakeTrayBackendLifecycle : IBackendLifecycle
    {
        private readonly Queue<LifecycleOperationResult> _results;

        public FakeTrayBackendLifecycle(params LifecycleOperationResult[] results)
        {
            _results = new Queue<LifecycleOperationResult>(results);
        }

        public Task<LifecycleOperationResult> EnsureRunningAsync()
        {
            return Task.FromResult(DequeueOrSuccess());
        }

        public Task<LifecycleOperationResult> RestartAsync()
        {
            return Task.FromResult(DequeueOrSuccess());
        }

        public Task<LifecycleOperationResult> StopAsync()
        {
            return Task.FromResult(DequeueOrSuccess());
        }

        private LifecycleOperationResult DequeueOrSuccess()
        {
            return _results.Count > 0 ? _results.Dequeue() : LifecycleOperationResult.Success();
        }
    }

    private sealed class FakeTrayShellPresenter : ITrayShellPresenter
    {
        public bool ShowPanelCalled { get; private set; }

        public bool RequestCloseCalled { get; private set; }

        public CloseIntent? RequestCloseIntent { get; private set; }

        public void ShowPanel()
        {
            ShowPanelCalled = true;
        }

        public void RequestClose(CloseIntent intent)
        {
            RequestCloseCalled = true;
            RequestCloseIntent = intent;
        }
    }

    private sealed class FakeShutdownConfirmation : IShutdownConfirmation
    {
        private readonly bool _shouldConfirm;

        public FakeShutdownConfirmation(bool shouldConfirm)
        {
            _shouldConfirm = shouldConfirm;
        }

        public bool ConfirmStopBroadcastAndExit(IWin32Window? owner = null)
        {
            return _shouldConfirm;
        }
    }
}
