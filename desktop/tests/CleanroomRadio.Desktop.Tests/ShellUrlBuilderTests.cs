using CleanroomRadio.Desktop.Shell;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class ShellUrlBuilderTests
{
    [Fact]
    public void BuildAppUrl_TargetsLocalPanelRoute()
    {
        var url = ShellUrlBuilder.BuildAppUrl("http://127.0.0.1:8787");

        Assert.Equal("http://127.0.0.1:8787/app", url);
    }
}
