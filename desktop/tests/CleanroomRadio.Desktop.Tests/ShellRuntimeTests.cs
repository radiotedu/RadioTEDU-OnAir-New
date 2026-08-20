using CleanroomRadio.Desktop.Shell;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class ShellClosePolicyTests
{
    [Fact]
    public void ShouldHideOnUserClose_ReturnsFalseWhenTrayModeIsDisabled()
    {
        var shouldHide = ShellClosePolicy.ShouldHideOnUserClose(
            CloseIntent.HideToTray,
            hideToTrayEnabled: false);

        Assert.False(shouldHide);
    }

    [Fact]
    public void ShouldHideOnUserClose_ReturnsTrueWhenTrayModeIsEnabledAndHideIsRequested()
    {
        var shouldHide = ShellClosePolicy.ShouldHideOnUserClose(
            CloseIntent.HideToTray,
            hideToTrayEnabled: true);

        Assert.True(shouldHide);
    }
}

public class ShellEnvironmentTests
{
    [Fact]
    public void FromEnvironment_UsesDefaultsWhenUnset()
    {
        var environment = new Dictionary<string, string?>();

        var settings = ShellEnvironmentSettings.FromEnvironment(environment);

        Assert.Equal(new Uri("http://127.0.0.1:8100/app"), settings.PanelUri);
    }

    [Fact]
    public void FromEnvironment_ThrowsForNonLoopbackHost()
    {
        var environment = new Dictionary<string, string?>
        {
            ["RADIOTEDU_ONAIR_HOST"] = "example.org",
        };

        var exception = Assert.Throws<FormatException>(() =>
            ShellEnvironmentSettings.FromEnvironment(environment));

        Assert.Contains("RADIOTEDU_ONAIR_HOST", exception.Message);
    }
}
