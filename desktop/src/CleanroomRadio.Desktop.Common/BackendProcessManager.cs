using System.Diagnostics;
using System.Globalization;

namespace CleanroomRadio.Desktop.Common;

public static class BackendProcessManager
{
    public static ProcessStartInfo CreateStartInfo(BackendOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);

        var executablePath = Path.GetFullPath(options.ExecutablePath);
        var startInfo = new ProcessStartInfo
        {
            FileName = executablePath,
            WorkingDirectory = Path.GetDirectoryName(executablePath) ?? string.Empty,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };

        startInfo.EnvironmentVariables["CLEANROOM_OPEN_PANEL"] = "0";
        startInfo.EnvironmentVariables["CLEANROOM_HOST"] = "127.0.0.1";
        startInfo.EnvironmentVariables["CLEANROOM_PORT"] = options.Port.ToString(CultureInfo.InvariantCulture);
        startInfo.EnvironmentVariables["CLEANROOM_DB_PATH"] = options.DatabasePath;
        // Keep the frozen backend's writable runtime data (AI cache, generated
        // state, and logs) under ProgramData instead of the read-only install
        // directory.  Without this, the packaged backend can crash on startup
        // when it tries to create _internal/data/ai_cache.
        startInfo.EnvironmentVariables["CLEANROOM_DATA_ROOT"] = options.DataRoot;
        // Use one machine-wide credential/configuration root so the operator
        // agent and any LocalSystem continuity host resolve the same DPAPI
        // machine-scoped Icecast credentials after upgrades or reboots.
        startInfo.EnvironmentVariables["CLEANROOM_USER_CONFIG_ROOT"] = options.DataRoot;

        return startInfo;
    }
}
