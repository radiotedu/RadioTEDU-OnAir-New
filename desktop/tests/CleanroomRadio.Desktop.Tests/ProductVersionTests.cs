using System.Reflection;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public sealed class ProductVersionTests
{
    [Fact]
    public void DesktopAssembliesConsumeRepositoryProductVersion()
    {
        var assembly = Assembly.GetExecutingAssembly();
        var root = new DirectoryInfo(AppContext.BaseDirectory);
        while (root is not null && !File.Exists(Path.Combine(root.FullName, "VERSION")))
            root = root.Parent;
        Assert.NotNull(root);
        var sourceVersion = File.ReadAllText(Path.Combine(root!.FullName, "VERSION")).Trim();
        var expected = Version.Parse(sourceVersion);
        Assert.Equal(new Version(expected.Major, expected.Minor, expected.Build, 0), assembly.GetName().Version);

        var informational = assembly
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?
            .InformationalVersion;
        Assert.StartsWith(sourceVersion, informational, StringComparison.Ordinal);
    }
}
