using CleanroomRadio.Desktop.Agent;
using CleanroomRadio.Desktop.Common;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class BackendLifecycleControllerTests
{
    [Fact]
    public async Task RestartAsync_AttachesToMatchingBackendAndStartsFreshInstance()
    {
        var existingHandle = new FakeBackendProcessHandle();
        var replacementHandle = new FakeBackendProcessHandle();
        var gateway = new FakeBackendProcessGateway(existingHandle, startProcess: replacementHandle);
        var healthProbe = new FakeBackendHealthProbe(false, true);
        var controller = CreateController(gateway, healthProbe);

        var result = await controller.RestartAsync();

        Assert.True(result.Succeeded);
        Assert.Null(result.ErrorMessage);
        Assert.Equal(1, gateway.FindMatchingProcessCalls);
        Assert.Equal(1, gateway.StartCalls);
        Assert.True(healthProbe.LastWaitRetries >= 120);
        Assert.True(existingHandle.GracefulStopCalled);
        Assert.False(existingHandle.ForceKillCalled);
        Assert.True(existingHandle.Disposed);
        Assert.True(replacementHandle.Started);
        Assert.False(replacementHandle.GracefulStopCalled);
        Assert.False(replacementHandle.ForceKillCalled);
    }

    [Fact]
    public async Task RestartAsync_FailsExplicitlyWhenBackendIsHealthyButNotOwned()
    {
        var gateway = new FakeBackendProcessGateway();
        var healthProbe = new FakeBackendHealthProbe(true);
        var controller = CreateController(gateway, healthProbe);

        var result = await controller.RestartAsync();

        Assert.False(result.Succeeded);
        Assert.Contains("already running", result.ErrorMessage, StringComparison.OrdinalIgnoreCase);
        Assert.Equal(0, gateway.StartCalls);
    }

    [Fact]
    public async Task StopAsync_ReturnsFailureWhenBackendStaysHealthyAfterStop()
    {
        var existingHandle = new FakeBackendProcessHandle
        {
            GracefulStopShouldSucceed = false,
        };
        var gateway = new FakeBackendProcessGateway(existingHandle);
        var healthProbe = new FakeBackendHealthProbe(true);
        var controller = CreateController(gateway, healthProbe);

        var result = await controller.StopAsync();

        Assert.False(result.Succeeded);
        Assert.Contains("still running", result.ErrorMessage, StringComparison.OrdinalIgnoreCase);
        Assert.True(existingHandle.GracefulStopCalled);
        Assert.True(existingHandle.ForceKillCalled);
        Assert.True(existingHandle.Disposed);
    }

    [Fact]
    public async Task StopAsync_UsesGracefulStopWithoutForceKillWhenBackendStopsCleanly()
    {
        var existingHandle = new FakeBackendProcessHandle();
        var gateway = new FakeBackendProcessGateway(existingHandle);
        var healthProbe = new FakeBackendHealthProbe(false);
        var controller = CreateController(gateway, healthProbe);

        var result = await controller.StopAsync();

        Assert.True(result.Succeeded);
        Assert.True(existingHandle.GracefulStopCalled);
        Assert.False(existingHandle.ForceKillCalled);
        Assert.True(existingHandle.Disposed);
    }

    private static BackendLifecycleController CreateController(
        FakeBackendProcessGateway gateway,
        FakeBackendHealthProbe healthProbe)
    {
        var options = new BackendOptions(
            @"C:\RadioTEDU OnAir\backend\RadioTEDU-OnAir-Backend.exe",
            @"C:\RadioTEDU OnAir\data",
            8100);

        return new BackendLifecycleController(options, gateway, healthProbe);
    }

    private sealed class FakeBackendProcessGateway : IBackendProcessGateway
    {
        private readonly Queue<FakeBackendProcessHandle?> _matchingProcesses;

        private readonly FakeBackendProcessHandle? _startProcess;

        public FakeBackendProcessGateway(
            FakeBackendProcessHandle? matchingProcess = null,
            FakeBackendProcessHandle? startProcess = null)
        {
            _matchingProcesses = new Queue<FakeBackendProcessHandle?>();
            if (matchingProcess is not null)
            {
                _matchingProcesses.Enqueue(matchingProcess);
            }

            _startProcess = startProcess;
        }

        public int FindMatchingProcessCalls { get; private set; }

        public int StartCalls { get; private set; }

        public IBackendProcessHandle? FindMatchingProcess(string executablePath)
        {
            FindMatchingProcessCalls++;
            return _matchingProcesses.Count > 0 ? _matchingProcesses.Dequeue() : null;
        }

        public IBackendProcessHandle Start(BackendOptions options)
        {
            StartCalls++;
            var handle = _startProcess ?? new FakeBackendProcessHandle();
            handle.MarkStarted();
            return handle;
        }
    }

    private sealed class FakeBackendHealthProbe : IBackendHealthProbe
    {
        private readonly Queue<bool> _healthStates;

        public FakeBackendHealthProbe(params bool[] healthStates)
        {
            _healthStates = new Queue<bool>(healthStates);
        }

        public Task<bool> IsHealthyAsync(Uri baseAddress, CancellationToken cancellationToken = default)
        {
            return Task.FromResult(DequeueHealthState());
        }

        public Task<bool> WaitForHealthyAsync(
            Uri baseAddress,
            int retries = 30,
            TimeSpan? delay = null,
            CancellationToken cancellationToken = default)
        {
            LastWaitRetries = retries;
            return Task.FromResult(DequeueHealthState());
        }

        public int LastWaitRetries { get; private set; }

        private bool DequeueHealthState()
        {
            return _healthStates.Count > 0 && _healthStates.Dequeue();
        }
    }

    private sealed class FakeBackendProcessHandle : IBackendProcessHandle
    {
        public bool Started { get; private set; }

        public bool GracefulStopCalled { get; private set; }

        public bool ForceKillCalled { get; private set; }

        public bool GracefulStopShouldSucceed { get; set; } = true;

        public bool Disposed { get; private set; }

        public bool HasExited { get; private set; }

        public void MarkStarted()
        {
            Started = true;
        }

        public bool TryGracefulStop(TimeSpan gracefulTimeout)
        {
            GracefulStopCalled = true;
            HasExited = GracefulStopShouldSucceed;
            return GracefulStopShouldSucceed;
        }

        public void ForceKill()
        {
            ForceKillCalled = true;
            HasExited = true;
        }

        public void Dispose()
        {
            Disposed = true;
        }
    }
}
