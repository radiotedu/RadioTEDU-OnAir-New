using System.IO;

namespace CleanroomRadio.Desktop.Common;

public sealed record BackendOptions(
    string ExecutablePath,
    string DataRoot,
    int Port)
{
    public string DatabasePath => Path.Combine(DataRoot, "cleanroom.db");
}
