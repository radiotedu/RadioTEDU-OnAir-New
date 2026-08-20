namespace CleanroomRadio.Desktop.Shell;

public static class ShellUrlBuilder
{
    public static string BuildAppUrl(string baseUrl)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(baseUrl);

        return $"{baseUrl.TrimEnd('/')}/app";
    }
}
