using CleanroomRadio.Desktop.Agent;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class TrayMenuModelTests
{
    [Fact]
    public void BuildDefault_ReturnsBaselineTrayCommandsInOrder()
    {
        var menu = TrayMenuModel.BuildDefault();

        Assert.Equal(
            new[]
            {
                "Open Panel",
                "Restart Backend",
                "Stop Broadcast and Exit",
            },
            menu.Items.Select(item => item.Text).ToArray());

        Assert.Equal(
            new[]
            {
                TrayCommand.OpenPanel,
                TrayCommand.RestartBackend,
                TrayCommand.StopBroadcastAndExit,
            },
            menu.Items.Select(item => item.Command).ToArray());
    }
}
