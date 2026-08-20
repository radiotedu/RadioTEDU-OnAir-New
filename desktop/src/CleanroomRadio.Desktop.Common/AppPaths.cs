using System.IO;

namespace CleanroomRadio.Desktop.Common;

public sealed record AppPaths(string InstallRoot)
{
    public static AppPaths ForScope(
        InstallScope scope,
        string localAppData,
        string programFiles)
    {
        var basePath = scope switch
        {
            InstallScope.CurrentUser => localAppData,
            InstallScope.AllUsers => programFiles,
            _ => throw new ArgumentOutOfRangeException(nameof(scope), scope, null),
        };

        return new AppPaths(Path.Combine(basePath, "RadioTEDU", "OnAir"));
    }
}
