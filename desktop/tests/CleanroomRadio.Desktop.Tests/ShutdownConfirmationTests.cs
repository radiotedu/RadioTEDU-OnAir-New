using CleanroomRadio.Desktop.Agent;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class ShutdownConfirmationTests
{
    [Fact]
    public void CreateStopBroadcastAndExitPrompt_ExplainsThatExitStopsTheRunningApp()
    {
        var prompt = ShutdownConfirmation.CreateStopBroadcastAndExitPrompt();

        Assert.Equal("Stop Broadcast and Exit", prompt.Title);
        Assert.Contains("stop the broadcast", prompt.Message, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("exit RadioTEDU OnAir", prompt.Message, StringComparison.OrdinalIgnoreCase);
    }
}
