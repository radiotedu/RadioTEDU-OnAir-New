using System.Reflection;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public sealed class ProductVersionTests
{
    [Fact]
    public void DesktopAssembliesConsumeRepositoryProductVersion()
    {
        var assembly = Assembly.GetExecutingAssembly();
        Assert.Equal(new Version(1, 0, 2, 0), assembly.GetName().Version);

        var informational = assembly
            .GetCustomAttribute<AssemblyInformationalVersionAttribute>()?
            .InformationalVersion;
        Assert.StartsWith("1.0.2", informational, StringComparison.Ordinal);
    }
}
