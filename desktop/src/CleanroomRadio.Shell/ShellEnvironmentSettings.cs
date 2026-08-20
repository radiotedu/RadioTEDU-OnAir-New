using System.Collections;
using System.Globalization;

namespace CleanroomRadio.Desktop.Shell;

public sealed record ShellEnvironmentSettings(Uri PanelUri, ShellLaunchMode LaunchMode)
{
    public static ShellEnvironmentSettings FromCurrentProcessEnvironment(string[]? commandLineArgs = null)
    {
        var environment = new Dictionary<string, string?>(StringComparer.OrdinalIgnoreCase);
        foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
        {
            if (entry.Key is string key)
            {
                environment[key] = entry.Value as string;
            }
        }

        return FromEnvironment(environment, commandLineArgs);
    }

    public static ShellEnvironmentSettings FromEnvironment(
        IReadOnlyDictionary<string, string?> environment,
        string[]? commandLineArgs = null)
    {
        ArgumentNullException.ThrowIfNull(environment);

        var host = GetValueOrDefault(
            environment,
            ShellProductIdentity.HostEnvironmentVariable,
            "127.0.0.1").Trim();
        if (host is not ("127.0.0.1" or "::1" or "localhost"))
        {
            throw new FormatException(
                $"{ShellProductIdentity.HostEnvironmentVariable} must be a loopback address.");
        }

        var port = ParsePort(
            environment,
            ShellProductIdentity.PortEnvironmentVariable,
            ShellProductIdentity.DefaultPort);
        var mode = ParseLaunchMode(
            GetValueOrDefault(
                environment,
                ShellProductIdentity.ShellModeEnvironmentVariable,
                "operator"),
            commandLineArgs ?? Array.Empty<string>());
        var uriHost = host == "::1" ? "[::1]" : host;
        var baseUri = new Uri($"http://{uriHost}:{port}");

        var panelUri = new Uri(ShellUrlBuilder.BuildAppUrl(baseUri.AbsoluteUri));
        return new ShellEnvironmentSettings(panelUri, mode);
    }

    private static string GetValueOrDefault(
        IReadOnlyDictionary<string, string?> environment,
        string key,
        string defaultValue)
    {
        if (!environment.TryGetValue(key, out var value) || string.IsNullOrWhiteSpace(value))
        {
            return defaultValue;
        }

        return value;
    }

    private static int ParsePort(
        IReadOnlyDictionary<string, string?> environment,
        string key,
        string defaultValue)
    {
        var value = GetValueOrDefault(environment, key, defaultValue);
        if (!int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var port) ||
            port is < 1 or > 65535)
        {
            throw new FormatException($"{key} must be a valid TCP port.");
        }

        return port;
    }

    private static ShellLaunchMode ParseLaunchMode(string configured, IReadOnlyList<string> commandLineArgs)
    {
        var selected = configured.Trim().ToLowerInvariant() switch
        {
            "" or "operator" => ShellLaunchMode.Operator,
            _ => throw new FormatException(
                $"{ShellProductIdentity.ShellModeEnvironmentVariable} must be 'operator'."),
        };

        foreach (var argument in commandLineArgs)
        {
            if (string.Equals(argument, "--operator", StringComparison.OrdinalIgnoreCase))
            {
                selected = ShellLaunchMode.Operator;
            }
            else
            {
                throw new FormatException("Only the --operator launch argument is supported.");
            }
        }

        return selected;
    }
}
