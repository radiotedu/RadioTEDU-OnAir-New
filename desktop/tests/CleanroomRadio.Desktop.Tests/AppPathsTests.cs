using CleanroomRadio.Desktop.Common;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class AppPathsTests
{
    [Fact]
    public void CurrentUserScope_UsesLocalAppData()
    {
        var paths = AppPaths.ForScope(
            InstallScope.CurrentUser,
            @"C:\Users\demo\AppData\Local",
            @"C:\Program Files");

        Assert.Equal(
            @"C:\Users\demo\AppData\Local\RadioTEDU\OnAir",
            paths.InstallRoot);
    }

    [Fact]
    public void AllUsersScope_UsesProgramFiles()
    {
        var paths = AppPaths.ForScope(
            InstallScope.AllUsers,
            @"C:\Users\demo\AppData\Local",
            @"C:\Program Files");

        Assert.Equal(
            @"C:\Program Files\RadioTEDU\OnAir",
            paths.InstallRoot);
    }

    [Fact]
    public void UnknownScope_ThrowsOutOfRangeException()
    {
        var scope = (InstallScope)123;

        var exception = Assert.Throws<ArgumentOutOfRangeException>(() =>
            AppPaths.ForScope(
                scope,
                @"C:\Users\demo\AppData\Local",
                @"C:\Program Files"));

        Assert.Equal(nameof(scope), exception.ParamName);
    }
}
