using CleanroomRadio.Desktop.Agent;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class ProcessBackendProcessHandleTests
{
    [Fact]
    public void TryGracefulStop_ReturnsTrueWhenProcessExitsWithoutForceKill()
    {
        var control = new FakeProcessControl
        {
            CloseMainWindowResult = true,
            WaitForExitResult = true,
        };
        var handle = new ProcessBackendProcessHandle(control);

        var stopped = handle.TryGracefulStop(TimeSpan.FromSeconds(1));

        Assert.True(stopped);
        Assert.True(control.CloseMainWindowCalled);
        Assert.True(control.WaitForExitCalled);
        Assert.False(control.KillCalled);
    }

    [Fact]
    public void TryGracefulStop_ReturnsFalseWhenProcessDoesNotExitGracefully()
    {
        var control = new FakeProcessControl
        {
            CloseMainWindowResult = false,
            WaitForExitResult = false,
        };
        var handle = new ProcessBackendProcessHandle(control);

        var stopped = handle.TryGracefulStop(TimeSpan.Zero);

        Assert.False(stopped);
        Assert.True(control.CloseMainWindowCalled);
        Assert.True(control.WaitForExitCalled);
        Assert.False(control.KillCalled);
    }

    [Fact]
    public void ForceKill_KillsAndWaitsForExit()
    {
        var control = new FakeProcessControl();
        var handle = new ProcessBackendProcessHandle(control);

        handle.ForceKill();

        Assert.True(control.KillCalled);
        Assert.True(control.WaitForExitCalled);
    }

    private sealed class FakeProcessControl : IProcessControl
    {
        public bool CloseMainWindowCalled { get; private set; }

        public bool WaitForExitCalled { get; private set; }

        public bool KillCalled { get; private set; }

        public bool CloseMainWindowResult { get; set; }

        public bool WaitForExitResult { get; set; }

        public bool HasExited { get; set; }

        public bool CloseMainWindow()
        {
            CloseMainWindowCalled = true;
            return CloseMainWindowResult;
        }

        public bool WaitForExit(TimeSpan timeout)
        {
            WaitForExitCalled = true;
            HasExited = WaitForExitResult;
            return WaitForExitResult;
        }

        public void Kill(bool entireProcessTree)
        {
            KillCalled = true;
            HasExited = true;
        }

        public void Dispose()
        {
        }
    }
}
