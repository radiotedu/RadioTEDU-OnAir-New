using CleanroomRadio.Desktop.Common;
using Xunit;

namespace CleanroomRadio.Desktop.Tests;

public class BackendProcessManagerTests
{
    [Fact]
    public void CreateStartInfo_HidesConsolePinsWorkingDirectoryAndSetsWrapperEnvironment()
    {
        var options = new BackendOptions(
            @"C:\RadioTEDU OnAir\backend\RadioTEDU-OnAir-Backend.exe",
            @"C:\RadioTEDU OnAir\data",
            8100);

        var startInfo = BackendProcessManager.CreateStartInfo(options);

        Assert.Equal(options.ExecutablePath, startInfo.FileName);
        Assert.True(startInfo.CreateNoWindow);
        Assert.False(startInfo.UseShellExecute);
        Assert.Equal(@"C:\RadioTEDU OnAir\backend", startInfo.WorkingDirectory);
        Assert.Equal(@"0", startInfo.EnvironmentVariables["CLEANROOM_OPEN_PANEL"]);
        Assert.Equal(@"127.0.0.1", startInfo.EnvironmentVariables["CLEANROOM_HOST"]);
        Assert.Equal(@"8100", startInfo.EnvironmentVariables["CLEANROOM_PORT"]);
        Assert.Equal(@"C:\RadioTEDU OnAir\data\cleanroom.db", startInfo.EnvironmentVariables["CLEANROOM_DB_PATH"]);
        Assert.Equal(@"C:\RadioTEDU OnAir\data", startInfo.EnvironmentVariables["CLEANROOM_DATA_ROOT"]);
        Assert.Equal(@"C:\RadioTEDU OnAir\data", startInfo.EnvironmentVariables["CLEANROOM_USER_CONFIG_ROOT"]);
    }

    [Theory]
    [InlineData("RadioTEDU-OnAir-Backend.exe")]
    [InlineData(@"tools\RadioTEDU-OnAir-Backend.exe")]
    public void CreateStartInfo_NormalizesRelativeExecutablePathAndPinsWorkingDirectory(
        string executablePath)
    {
        var previousCurrentDirectory = Directory.GetCurrentDirectory();
        var tempRoot = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(tempRoot);
        Directory.SetCurrentDirectory(tempRoot);

        try
        {
            var options = new BackendOptions(
                executablePath,
                Path.Combine(tempRoot, "data"),
                8100);

            var startInfo = BackendProcessManager.CreateStartInfo(options);

            var expectedFileName = Path.GetFullPath(executablePath);
            Assert.Equal(expectedFileName, startInfo.FileName);
            Assert.Equal(Path.GetDirectoryName(expectedFileName), startInfo.WorkingDirectory);
        }
        finally
        {
            Directory.SetCurrentDirectory(previousCurrentDirectory);
            Directory.Delete(tempRoot, recursive: true);
        }
    }
}
