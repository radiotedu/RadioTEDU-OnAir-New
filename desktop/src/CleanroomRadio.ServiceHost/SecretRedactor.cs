using System.Text.RegularExpressions;

namespace CleanroomRadio.ServiceHost;

public static partial class SecretRedactor
{
    [GeneratedRegex("(?i)(password|passwd|pwd|token|secret|api[_-]?key|authorization)\\s*(?:=|:)\\s*(?:(?:bearer\\s+)?(?:<redacted>|\\\"[^\\\"]*\\\"|'[^']*'|[^\\s,;]+))", RegexOptions.CultureInvariant)]
    private static partial Regex KeyValueSecret();

    [GeneratedRegex("(?i)(bearer)\\s+[A-Za-z0-9._~+/-]+=*", RegexOptions.CultureInvariant)]
    private static partial Regex BearerSecret();

    public static string Redact(string? value)
    {
        if (string.IsNullOrEmpty(value))
        {
            return string.Empty;
        }

        return KeyValueSecret().Replace(BearerSecret().Replace(value, "$1 <redacted>"), "$1=<redacted>");
    }
}
